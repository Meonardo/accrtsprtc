#!/usr/bin/python
from http.server import BaseHTTPRequestHandler, HTTPServer
from os import curdir, sep
from collections import namedtuple

import os
import cgi
import json
import argparse
import subprocess
import signal

ResponseStatus = namedtuple("HTTPStatus",
                            ["code", "message"])

HTTP_STATUS = {"OK": ResponseStatus(code=200, message="OK"),
               "BAD_REQUEST": ResponseStatus(code=400, message="Bad request"),
               "NOT_FOUND": ResponseStatus(code=404, message="Not found"),
               "INTERNAL_SERVER_ERROR": ResponseStatus(code=500, message="Internal server error")}

ROUTE_INDEX = "/index.html"
ROUTE_STOP = "/camera/push/stop"
ROUTE_START = "/camera/push/start"

RTSP_ = ""
JANUS_PROCESS = None

class HTTPStatusError(Exception):
    """Exception wrapping a value from http.server.HTTPStatus"""

    def __init__(self, status, description=None):
        """
        Constructs an error instance from a tuple of
        (code, message, description), see http.server.HTTPStatus
        """
        super(HTTPStatusError, self).__init__()
        self.code = status.code
        self.message = status.message
        self.explain = description

#This class will handles any incoming request from
#the browser 
class RequestHandler(BaseHTTPRequestHandler):

    # 404 Not found.
    def route_not_found(self, path, query):
        """Handles routing for unexpected paths"""
        raise HTTPStatusError(HTTP_STATUS["NOT_FOUND"], "Page not found")

    # Handler for the GET requests
    def do_GET(self):
        print("Current requesting path: %s", self.path)

        path, _, query_string = self.path.partition('?')
        query_components = dict(qc.split("=") for qc in query_string.split("&"))

        response = None

        print(u"[START]: Received GET for %s with query: %s" % (path, query_components))

        try:
            if path == ROUTE_INDEX:
                self.send_response(200)
                self.send_header('Content-type','text/html')
                self.end_headers()
                # Send the html message
                self.wfile.write("RTSP Stream push to Janus!".encode())
            else:
                response = self.route_not_found(path, query_components)
        except HTTPStatusError as err:
            self.send_error(err.code, err.message)

        print("[END]")

        return

    # Handler for the POST requests
    def do_POST(self):
        path, _,_ = self.path.partition('?')

        print(u"[START]: Received POST for %s" % path)

        try: 
            fs = cgi.FieldStorage(
                fp = self.rfile, 
                headers = self.headers,
                environ = {'REQUEST_METHOD':'POST',
                         'CONTENT_TYPE':self.headers['Content-Type'],
            })

            form = {}
            for field in fs.list or ():
                form[field.name] = field.value

            print("In coming form: ", form)

            if path == ROUTE_START:
                r = self.check_start(form)
                self.send_json_response(r)

            elif path == ROUTE_STOP:
                r = self.check_stop(form)
                self.send_json_response(r)

        except HTTPStatusError as err:
            self.send_error(err.code, err.message)

        print("[END]")

        return
                      
    # Send a JSON Response.
    def send_json_response(self, json_dict):
        self.send_response(200)
        self.send_header('Content-type','application/json')
        self.end_headers()
        obj = json.dumps(json_dict, indent = 4)
        self.wfile.write( obj.encode(encoding='utf_8') )
        return

    # Common Response   
    def comm_response(self, success, code, data):
        r = {"success": success, "code": code, "data": data}
        return r

    # check start command 
    def check_start(self, form):
        display = form["display"] or "LocalCamera"
        room = form["room"]
        if room.isdigit() == False:
            return self.comm_response(False, -1, "Please input correct Room number!")

        rtsp = form["rtsp"]
        if len(rtsp) == 0:
            return self.comm_response(False, -2, "Please input correct RTSP address!")

        janus_signaling = form["janus"]

        global RTSP_
        if rtsp != RTSP_:
            RTSP_ = rtsp
            self.launch_janus(rtsp, room, display, janus_signaling)
            msg = rtsp + " has been published to VideoRoom " + room
            return self.comm_response(True, 1, msg)

        return self.comm_response(False, -3, "You've published the stream!")

    # Launch Janus from janus.py 
    def launch_janus(self, rtsp, room, display, janus_signaling='ws://127.0.0.1:8188'):
        dir_path = os.path.dirname(os.path.realpath(__file__))
        janus_path = dir_path + "/janus.py"
        global JANUS_PROCESS
        JANUS_PROCESS = subprocess.Popen(['python3', janus_path, janus_signaling, '--play-from', rtsp, '--name', display, '--room', room, '--verbose'])

    # Check stop command
    def check_stop(self, form):
        global RTSP_
        global JANUS_PROCESS
        rtsp = form["rtsp"]
        if len(rtsp) == 0:
            return self.comm_response(False, -2, "Please input correct RTSP address!")
        if rtsp != RTSP_:
            return self.comm_response(False, -4, "No RTSP stream published!")
        if JANUS_PROCESS != None:
            print("Stopping SubProcess.")
            RTSP_ = ""
            # JANUS_PROCESS.terminate()
            os.kill(JANUS_PROCESS.pid, signal.SIGINT)

            JANUS_PROCESS = None
            msg = rtsp + " Stopped!"
            return self.comm_response(True, 1, msg)
        return self.comm_response(False, -5, "No subproc Found!")
        

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="accrtsprtc")
    parser.add_argument(
        "--p",
        type=int,
        default=9001,
        help="HTTP port number, default is 9001",
    )
    args = parser.parse_args()

    try:
        #Create a web server and define the handler to manage the
        #incoming request
        server = HTTPServer(('', args.p), RequestHandler)
        print('Started httpserver on port', args.p)

        #Wait forever for incoming htto requests
        server.serve_forever()

    except KeyboardInterrupt:
        print('^C received, shutting down the web server')
        if JANUS_PROCESS != None:
            os.kill(JANUS_PROCESS.pid, signal.SIGINT)
            JANUS_PROCESS = None
        
        server.socket.close()