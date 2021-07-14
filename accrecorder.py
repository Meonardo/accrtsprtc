#!/usr/bin/python
import os
import cgi
import argparse
import subprocess
import signal
import string
import websockets
import json
import attr
import random
import asyncio

from http.server import BaseHTTPRequestHandler, HTTPServer
from os import curdir, sep
from collections import namedtuple
from websockets.exceptions import ConnectionClosed
from aiortc import RTCPeerConnection, RTCSessionDescription
from aiortc.contrib.media import MediaPlayer, MediaRecorder

ResponseStatus = namedtuple("HTTPStatus",
                            ["code", "message"])

HTTP_STATUS = {"OK": ResponseStatus(code=200, message="OK"),
               "BAD_REQUEST": ResponseStatus(code=400, message="Bad request"),
               "NOT_FOUND": ResponseStatus(code=404, message="Not found"),
               "INTERNAL_SERVER_ERROR": ResponseStatus(code=500, message="Internal server error")}

ROUTE_INDEX = "/index.html"
# 停止录制
ROUTE_STOP = "/record/stop"
# 开始录制
ROUTE_START = "/record/start"

# 对应 Janus 房间内 发布者 ID
SCREEN_ID = 1
CAM_TEARCHER_ID = 100
CAM_STUDENT_ID = 101

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
        path, _, query_string = self.path.partition('?')
        query_components = dict(qc.split("=") for qc in query_string.split("&"))

        print(u"[START]: Received GET for %s with query: %s" % (path, query_components))
        try:
            if path == ROUTE_INDEX:
                self.send_response(200)
                self.send_header('Content-type','text/html')
                self.end_headers()
                # Send the html message
                self.wfile.write("Recording the conference!".encode())
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
        return self.comm_response(False, -3, "You've published the stream!")

    # Launch Janus from janus.py 
    def launch_janus(self, rtsp, room, display, janus_signaling='ws://127.0.0.1:8188'):
        dir_path = os.path.dirname(os.path.realpath(__file__))
        janus_path = dir_path + "/janus.py"
        global JANUS_PROCESS
        JANUS_PROCESS = subprocess.Popen(['python3', janus_path, janus_signaling, '--play-from', rtsp, '--name', display, '--room', room, '--verbose'])

    # Check stop command
    def check_stop(self, form):
        return self.comm_response(False, -5, "No subproc Found!")

# Random Transaction ID        
def transaction_id():
    return "".join(random.choice(string.ascii_letters) for x in range(12))

@attr.s
class JanusEvent:
    sender = attr.ib(validator=attr.validators.instance_of(int))

@attr.s
class PluginData(JanusEvent):
    plugin = attr.ib(validator=attr.validators.instance_of(str))
    data = attr.ib()
    jsep = attr.ib()

@attr.s
class WebrtcUp(JanusEvent):
    pass

@attr.s
class Media(JanusEvent):
    receiving = attr.ib(validator=attr.validators.instance_of(bool))
    kind = attr.ib(validator=attr.validators.in_(["audio", "video"]))

    @kind.validator
    def validate_kind(self, attribute, kind):
        if kind not in ["video", "audio"]:
            raise ValueError("kind must equal video or audio")

@attr.s
class SlowLink(JanusEvent):
    uplink = attr.ib(validator=attr.validators.instance_of(bool))
    lost = attr.ib(validator=attr.validators.instance_of(int))

@attr.s
class HangUp(JanusEvent):
    reason = attr.ib(validator=attr.validators.instance_of(str))

@attr.s(cmp=False)
class Ack:
    transaction = attr.ib(validator=attr.validators.instance_of(str))

@attr.s
class Jsep:
    sdp = attr.ib()
    type = attr.ib(validator=attr.validators.in_(["offer", "pranswer", "answer", "rollback"]))

@attr.s
class JanusGateway:
    server = attr.ib(validator=attr.validators.instance_of(str))
    _messages = attr.ib(factory=set)

    async def connect(self):
        self.conn = await websockets.connect(self.server, subprotocols=['janus-protocol'])
        transaction = transaction_id()
        await self.conn.send(json.dumps({
            "janus": "create",
            "transaction": transaction
            }))
        resp = await self.conn.recv()
        print (resp)
        parsed = json.loads(resp)
        assert parsed["janus"] == "success", "Failed creating session"
        assert parsed["transaction"] == transaction, "Incorrect transaction"
        self.session = parsed["data"]["id"]

    async def close(self):
        await self.conn.close()

    async def attach(self, plugin):
        assert hasattr(self, "session"), "Must connect before attaching to plugin"
        transaction = transaction_id()
        await self.conn.send(json.dumps({
            "janus": "attach",
            "session_id": self.session,
            "plugin": plugin,
            "transaction": transaction
        }))
        resp = await self.conn.recv()
        parsed = json.loads(resp)
        assert parsed["janus"] == "success", "Failed attaching to {}".format(plugin)
        assert parsed["transaction"] == transaction, "Incorrect transaction"
        self.handle = parsed["data"]["id"]
    
    async def sendmessage(self, body, jsep=None):
        assert hasattr(self, "session"), "Must connect before sending messages"
        assert hasattr(self, "handle"), "Must attach before sending messages"
        transaction = transaction_id()
        janus_message = {
            "janus": "message",
            "session_id": self.session,
            "handle_id": self.handle,
            "transaction": transaction,
            "body": body
        }
        if jsep is not None:
            janus_message["jsep"] = jsep
        await self.conn.send(json.dumps(janus_message))

    async def keepalive(self):
        assert hasattr(self, "session"), "Must connect before sending messages"
        assert hasattr(self, "handle"), "Must attach before sending messages"

        while True:
            try:
                await asyncio.sleep(30)
                transaction = transaction_id()
                await self.conn.send(json.dumps({
                    "janus": "keepalive",
                    "session_id": self.session,
                    "handle_id": self.handle,
                    "transaction": transaction
                }))
            except KeyboardInterrupt:
                return

    async def recv(self):
        if len(self._messages) > 0:
            return self._messages.pop()
        else:
            return await self._recv_and_parse()

    async def _recv_and_parse(self):
        raw = json.loads(await self.conn.recv())
        print("Received: ", raw)
        janus = raw["janus"]

        if janus == "event":
            return PluginData(
                sender=raw["sender"],
                plugin=raw["plugindata"]["plugin"],
                data=raw["plugindata"]["data"],
                jsep=raw["jsep"] if "jsep" in raw else None
            )
        elif janus == "webrtcup":
            return WebrtcUp(
                sender=raw["sender"]
            )
        elif janus == "media":
            return Media(
                sender=raw["sender"],
                receiving=raw["receiving"],
                kind=raw["type"]
            )
        elif janus == "slowlink":
            return SlowLink(
                sender=raw["sender"],
                uplink=raw["uplink"],
                lost=raw["lost"]
            )
        elif janus == "hangup":
            return HangUp(
                sender=raw["sender"],
                reason=raw["reason"]
            )
        elif janus == "ack":
            return Ack(
                transaction=raw["transaction"]
            )
        else:
            return raw

    async def loop(self, room):
        await self.connect()
        await self.attach("janus.plugin.videoroom")

        loop = asyncio.get_event_loop()
        loop.create_task(self.keepalive())

        joinmessage = { "request": "join", "ptype": "subscriber", "room": room, "pin": str(room), "display": "RecordMachine" }
        await self.sendmessage(joinmessage)

        assert self.conn

        while True:
            try:
                msg = await self.recv()
                if isinstance(msg, PluginData):
                    await self.handle_plugin_data(msg)
                elif isinstance(msg, Media):
                    print (msg)
                elif isinstance(msg, WebrtcUp):
                    print (msg)
                elif isinstance(msg, SlowLink):
                    print (msg)
                elif isinstance(msg, HangUp):
                    print (msg)
                elif not isinstance(msg, Ack):
                    print(msg)
            except (KeyboardInterrupt, ConnectionClosed):
                return

        return 0

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="accrtsprtc")
    parser.add_argument(
        "--p",
        type=int,
        default=9002,
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