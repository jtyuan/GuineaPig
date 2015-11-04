import getopt
import sys
import os
import logging
import threading
import collections
import subprocess
import time
import Queue
import shutil
import urllib
import time
import traceback

##############################################################################
# Map Reduce Streaming for GuineaPig (mrs_gp) - very simple
# multi-threading map-reduce, to be used with inputs/outputs which are
# just files in directories.  This combines multi-threading and
# multi-processing.
#
# For i/o bound tasks the inputs should be on ramdisk.  
#
# To do:
#  map-only tasks, multiple map inputs
#  secondary grouping sort key --joinmode
#  optimize - keys
##############################################################################

##############################################################################
#
# shared "files system"
#
##############################################################################

class GPFileSystem(object):

    def __init__(self):
        #file names in directory/shards
        self.filesIn = collections.defaultdict(list)
        #content of (dir,file)
        self.linesOf = {}
    def rmDir(self,d0):
        d = self._fixDir(d0)
        if d in self.filesIn:
            for f in self.filesIn[d]:
                del self.linesOf[(d,f)]
            del self.filesIn[d]
    def append(self,d0,f,line):
        d = self._fixDir(d0)
        if not f in self.filesIn[d]:
            self.filesIn[d].append(f)
            self.linesOf[(d,f)] = list()
        self.linesOf[(d,f)].append(line)
    def listDirs(self):
        return self.filesIn.keys()
    def listFiles(self,d0):
        d = self._fixDir(d0)
        return self.filesIn[d]
    def cat(self,d0,f):
        d = self._fixDir(d0)
        return self.linesOf[(d,f)]
    def head(self,d0,f,n):
        d = self._fixDir(d0)
        return self.linesOf[(d,f)][:n]
    def tail(self,d0,f,n):
        d = self._fixDir(d0)
        return self.linesOf[(d,f)][-n:]
    def __str__(self):
        return "FS("+str(self.filesIn)+";"+str(self.linesOf)+")"
    def _fixDir(self,d):
        return d if not d.startswith("gpfs:") else d[len("gpfs:"):]

FS = GPFileSystem()

##############################################################################
# main map-reduce utilities
##############################################################################

def performTask(optdict):
    """Utility that calls mapreduce or maponly, as appropriate, based on the options."""
    indir = optdict['--input']
    outdir = optdict['--output']
    if '--reducer' in optdict:
        #usage 1: a basic map-reduce has --input, --output, --mapper, --reducer, and --numReduceTasks
        mapper = optdict.get('--mapper','cat')
        reducer = optdict.get('--reducer','cat')
        numReduceTasks = int(optdict.get('--numReduceTasks','1'))
        mapreduce(indir,outdir,mapper,reducer,numReduceTasks)
    else:
        #usage 1: a map-only task has --input, --output, --mapper
        mapper = optdict.get('--mapper','cat')
        maponly(indir,outdir,mapper)        

def mapreduce(indir,outdir,mapper,reducer,numReduceTasks):
    """Run a generic streaming map-reduce process.  The mapper and reducer
    are shell commands, as in Hadoop streaming.  Both indir and outdir
    are directories."""

    usingGPFS,infiles = setupFiles(indir,outdir)

    # Set up a place to save the inputs to K reducers - each of which
    # is a buffer bj, which maps a key to a list of values associated
    # with that key.  To fill these buffers we also have K threads tj
    # to accumulate inputs, and K Queue's qj for the threads to read
    # from.

    logging.info('starting reduce buffer queues')
    reducerQs = []        # task queues to join with later 
    reducerBuffers = []   # data to send to reduce processes later 
    for j in range(numReduceTasks):
        qj = Queue.Queue()
        bj = collections.defaultdict(list)
        reducerQs.append(qj)
        reducerBuffers.append(bj)
        tj = threading.Thread(target=acceptReduceInputs, args=(qj,bj))
        tj.daemon = True
        tj.start()

    # start the mappers - each of which is a process that reads from
    # an input file or GPFS location, and a thread that passes its
    # outputs to the reducer queues.

    logging.info('starting mapper processes and shuffler threads')
    mappers = []
    mapFeeders = []
    for fi in infiles:
        # WARNING: it doesn't seem to work well to start the processes
        # inside a thread - this led to bugs with the reducer
        # processes.  This is possibly a python library bug:
        # http://bugs.python.org/issue1404925
        if 'input' in usingGPFS:
            mapPipeI = subprocess.Popen(mapper,shell=True,stdin=subprocess.PIPE,stdout=subprocess.PIPE)
            feederI = threading.Thread(target=feedPipeFromGPFS, args=(indir,fi,mapPipeI))
            feederI.start()
            mapFeeders.append(feederI)
        else:
            mapPipeI = subprocess.Popen(mapper,shell=True,stdin=open(indir + "/" + fi),stdout=subprocess.PIPE)
        si = threading.Thread(target=shuffleMapOutputs, args=(mapper,mapPipeI,reducerQs,numReduceTasks))
        si.start()                      # si will join the mapPipe process
        mappers.append(si)

    #wait for the map tasks, and to empty the queues
    joinAll(mappers,'mappers')        
    if mapFeeders: joinAll(mapFeeders,'map feeders') 

    # run the reduce processes, each of which is associated with a
    # thread that feeds it inputs from the j's reduce buffer.

    logging.info('starting reduce processes and threads to feed these processes')
    reducers = []
    reducerConsumers = []
    for j in range(numReduceTasks):    
        if 'output' in usingGPFS:
            reducePipeJ = subprocess.Popen(reducer,shell=True,stdin=subprocess.PIPE,stdout=subprocess.PIPE)
            consumerJ = threading.Thread(target=writePipeToGPFS, args=(outdir,("part%05d" % j),reducePipeJ))
            consumerJ.start()
            reducerConsumers.append(consumerJ)
        else:
            fpj = open("%s/part%05d" % (outdir,j), 'w')
            reducePipeJ = subprocess.Popen(reducer,shell=True,stdin=subprocess.PIPE,stdout=fpj)
        uj = threading.Thread(target=sendReduceInputs, args=(reducerBuffers[j],reducePipeJ,j))
        uj.start()                      # uj will shut down reducePipeJ process on completion
        reducers.append(uj)

    #wait for the reduce tasks
    joinAll(reducerQs,'reduce queues')  # the queues have been emptied
    joinAll(reducers,'reducers')
    if reducerConsumers: joinAll(reducerConsumers,'reducer consumers')

def maponly(indir,outdir,mapper):
    """Like mapreduce but for a mapper-only process."""

    usingGPFS,infiles = setupFiles(indir,outdir)

    # start the mappers - each of which is a process that reads from
    # an input file, and outputs to the corresponding output file

    logging.info('starting mapper processes')
    activeMappers = set()
    mapFeeders = []
    mapConsumers = []
    for fi in infiles:
        if ('input' in usingGPFS) and not ('output' in usingGPFS):
            mapPipeI = subprocess.Popen(mapper,shell=True,stdin=subprocess.PIPE,stdout=open(outdir + "/" + fi, 'w'))
            feederI  = threading.Thread(target=feedPipeFromGPFS, args=(indir,fi,mapPipeI))
            feederI.start()
            mapFeeders.append(feederI)
        elif not ('input' in usingGPFS) and ('output' in usingGPFS):
            mapPipeI = subprocess.Popen(mapper,shell=True,stdin=open(indir + "/" + fi),stdout=subprocess.PIPE)
            consumerI  = threading.Thread(target=writePipeToGPFS, args=(outdir,fi,mapPipeI))
            consumerI.start()
            mapConsumers.append(consumerI)
        elif ('input' in usingGPFS) and ('output' in usingGPFS):
            mapPipeI = subprocess.Popen(mapper,shell=True,stdin=subprocess.PIPE,stdout=subprocess.PIPE)
            feederI  = threading.Thread(target=feedPipeFromGPFS, args=(indir,fi,mapPipeI))
            feederI.start()
            mapFeeders.append(feederI)
            consumerI  = threading.Thread(target=writePipeToGPFS, args=(outdir,fi,mapPipeI))
            consumerI.start()
            mapConsumers.append(consumerI)
        else:
            mapPipeI = subprocess.Popen(mapper,shell=True,stdin=open(indir + "/" + fi),stdout=open(outdir + "/" + fi, 'w'))
        activeMappers.add(mapPipeI)

    #wait for the map tasks to finish
    for mapPipe in activeMappers:
        mapPipe.wait()
    if mapFeeders: joinAll(mapFeeders, 'map feeders')
    if mapConsumers: joinAll(mapConsumers, 'map consumers')

#
# subroutines
#

def setupFiles(indir,outdir):
    usingGPFS = set()
    if indir.startswith("gpfs:"):
        usingGPFS.add('input')
        infiles = FS.listFiles(indir)
    else:
        infiles = [f for f in os.listdir(indir)]
    if outdir.startswith("gpfs:"):
        usingGPFS.add('output')
        FS.rmDir(outdir)
    else:
        if os.path.exists(outdir):
            logging.warn('removing %s' % (outdir))
            shutil.rmtree(outdir)
        os.makedirs(outdir)
    logging.info('inputs: %d files from %s' % (len(infiles),indir))
    return usingGPFS,infiles

#
# routines attached to threads
#

def feedPipeFromGPFS(dirName,fileName,pipe):
    for line in FS.cat(dirName, fileName):
        pipe.stdin.write(line+"\n")

def writePipeToGPFS(dirName,fileName,pipe):
    for line in pipe.stdout:
        FS.append(dirName,fileName,line.strip())

def shuffleMapOutputs(mapper,mapPipe,reducerQs,numReduceTasks):
    """Thread that takes outputs of a map pipeline, hashes them, and
    sticks them on the appropriate reduce queue."""
    #maps shard index to a key->list defaultdict
    shufbuf = collections.defaultdict(lambda:collections.defaultdict(list))
    for line in mapPipe.stdout:
        k = key(line)
        h = hash(k) % numReduceTasks    # send to reducer buffer h
        shufbuf[h][k].append(line)
    logging.info('shuffleMapOutputs for '+str(mapPipe)+' sending buffer to reducerQs')
    for h in shufbuf:
        reducerQs[h].put(shufbuf[h])
    mapPipe.wait()                      # wait for termination of mapper process
    logging.info('shuffleMapOutputs for pipe '+str(mapPipe)+' done')

def accumulateReduceInputs_v1(reducerQ,reducerBuf):
    """Daemon thread that monitors a queue of items to add to a reducer
    input buffer.  Items in the buffer are grouped by key."""
    while True:
        (k,line) = reducerQ.get()
        reducerBuf[k].append(line)
        reducerQ.task_done()

def acceptReduceInputs(reducerQ,reducerBuf):
    """Daemon thread that monitors a queue of items to add to a reducer
    input buffer.  Items in the buffer are grouped by key."""
    while True:
        shufbuf = reducerQ.get()
        nLines = 0
        nKeys = 0
        for key,lines in shufbuf.items():
            nLines += len(lines)
            nKeys += 1
            reducerBuf[key].extend(lines)
        logging.info('acceptReduceInputs accepted %d lines for %d keys' % (nLines,nKeys))
        reducerQ.task_done()

def sendReduceInputs(reducerBuf,reducePipe,j):
    """Thread to send contents of a reducer buffer to a reduce process."""
    for (k,lines) in reducerBuf.items():
        for line in lines:
            reducePipe.stdin.write(line)
    reducePipe.stdin.close()
    reducePipe.wait()                   # wait for termination of reducer

#
# utils
#

# TODO make this faster

def key(line):
    """Extract the key for a line containing a tab-separated key,value pair."""
    return line[:line.find("\t")]

def joinAll(xs,msg):
    """Utility to join with all threads/queues in a list."""
    logging.info('joining ' + str(len(xs))+' '+msg)
    for i,x in enumerate(xs):
        x.join()
    logging.info('joined all '+msg)


##############################################################################
# server/client stuff
##############################################################################

# server

from BaseHTTPServer import BaseHTTPRequestHandler, HTTPServer
import urlparse

class MRSHandler(BaseHTTPRequestHandler):
    
    def _sendList(self,title,items):
        self.send_response(200)
        self.send_header('Content-type','text-html')
        self.end_headers()
        #print "leadin",leadin,"items",items
        itemList = ''
        if items:
            itemList = "\n".join(["<ul>"] + map(lambda it:"<li>%s" % it, items) + ["</ul>"])
        self.wfile.write("<html><head>%s</head>\n<body>\n%s%s\n</body></html>\n" % (title,title,itemList))

    def _sendFile(self,text):
        self.send_response(200)
        self.send_header('Content-type','text-plain')
        self.end_headers()
        self.wfile.write(text)

    def do_GET(self):
        print "GET request "+self.path
        try:
            p = urlparse.urlparse(self.path)
            requestOp = p.path
            requestArgs = urlparse.parse_qs(p.query)
            #convert the dict of lists to a dict of items, since I
            # don't use multiple values for any key
            requestArgs = dict(map(lambda (key,valueList):(key,valueList[0]), requestArgs.items()))
            print "request:",requestOp,requestArgs
            if requestOp=="ls" and not 'dir' in requestArgs:
                self._sendList("View listing",FS.listDirs())
            elif requestOp=="ls" and 'dir' in requestArgs:
                d = requestArgs['dir']
                self._sendList("Files in "+d,FS.listFiles(d))
            elif requestOp=="append":
                d = requestArgs['dir']
                f = requestArgs['file']
                line = requestArgs['line']
                FS.append(d,f,line)
                self._sendList("Appended to "+d+"/"+f,[line])
            elif requestOp=="cat":
                d = requestArgs['dir']
                f = requestArgs['file']
                self._sendFile("\n".join(FS.cat(d,f)))
            elif requestOp=="head":
                d = requestArgs['dir']
                f = requestArgs['file']
                n = requestArgs['n']
                self._sendFile("\n".join(FS.head(d,f,int(n))))
            elif requestOp=="tail":
                d = requestArgs['dir']
                f = requestArgs['file']
                n = requestArgs['n']
                self._sendFile("\n".join(FS.tail(d,f,int(n))))
            elif requestOp=="task":
                try:
                    start = time.time()
                    performTask(requestArgs)
                    end = time.time()
                    stat =  "Task performed in %.2f sec" % (end-start)
                    print stat
                    self._sendList(stat, map(str, requestArgs.items()))
                except Exception:
                    self._sendFile(traceback.format_exc())
            else:
                self._sendList("Error: unknown command "+requestOp,[self.path])
        except KeyError:
                self._sendList("Error: illegal command",[self.path])
  
def runServer():
    server_address = ('127.0.0.1', 1969)
    httpd = HTTPServer(server_address, MRSHandler)
    print('http server is running on port 1969...')
    httpd.serve_forever()

# client

import httplib
 
def sendRequest(command):
    http_server = "127.0.0.1:1969"
    conn = httplib.HTTPConnection(http_server)
    conn.request("GET", command)
    response = conn.getresponse()
    print(response.status, response.reason)
    data_received = response.read()
    print(data_received)
    conn.close()

##############################################################################
# main
##############################################################################

def usage():
    print "usage: --serve [PORT]"
    print "usage: --send command"
    print "usage: --task --input ..."
    print "usage: --input DIR1 --output DIR2 --mapper [SHELL_COMMAND]"
    print "       --input DIR1 --output DIR2 --mapper [SHELL_COMMAND] --reducer [SHELL_COMMAND] --numReduceTasks [K]"

if __name__ == "__main__":

    logging.basicConfig(level=logging.INFO)

    argspec = ["task", "serve", "send=", "input=", "output=", "mapper=", "reducer=", "numReduceTasks=", "joinInputs=", "help"]
    optlist,args = getopt.getopt(sys.argv[1:], 'x', argspec)
    optdict = dict(optlist)
    
    if "--serve" in optdict:
        runServer()
    if "--send" in optdict:
        sendRequest(optdict['--send'])
    elif "--task" in optdict:
        del optdict['--task']
        sendRequest("task?" + urllib.urlencode(optdict))
    elif "--help" in optdict or (not '--input' in optdict) or (not '--output' in optdict):
        usage()
    else:
        performTask(optdict)