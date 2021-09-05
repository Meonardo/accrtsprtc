#!/usr/bin/python3
import platform
import os
import argparse
import subprocess
import signal
import time
import json
import cgi
import queue
import threading
import asyncio

from pathlib import Path
from aiohttp import web
from multiprocessing.connection import Listener
from janus import print
from collections import namedtuple
from http.server import BaseHTTPRequestHandler, HTTPServer

CAMS = {}
# debug level 0 means nothing, 1 means debug
DEBUG_LEVEL = 1
ROOT = os.path.abspath(os.path.dirname(__file__))


ResponseStatus = namedtuple("HTTPStatus",
                            ["code", "message"])

HTTP_STATUS = {"OK": ResponseStatus(code=200, message="OK"),
               "BAD_REQUEST": ResponseStatus(code=400, message="Bad request"),
               "NOT_FOUND": ResponseStatus(code=404, message="Not found"),
               "INTERNAL_SERVER_ERROR": ResponseStatus(code=500, message="Internal server error")}

ROUTE_INDEX = "/index.html"
ROUTE_STOP = "/camera/push/stop"
ROUTE_START = "/camera/push/start"
ROUTE_PRIVATE_SUB = "/camera/subprocess"


class RTSPClient:
    def __init__(self, publisher, rtsp):
        self.publisher = publisher
        self.rtsp = rtsp
        self.process = None
        self.log_handler = None
        self.request_session = None
        self.queue = queue.Queue(3)


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


# This class will handles any incoming request from
# the browser
class RequestHandler(BaseHTTPRequestHandler):
    clients = {}
    debug_log_level = 1

    # 404 Not found.
    def route_not_found(self, path, query):
        """Handles routing for unexpected paths"""
        raise HTTPStatusError(HTTP_STATUS["NOT_FOUND"], "Page not found")

    # Handler for the GET requests
    def do_GET(self):
        print("Current requesting path: %s", self.path)

        path, _, query_string = self.path.partition('?')
        query_components = dict(qc.split("=") for qc in query_string.split("&"))

        print(u"[START]: Received GET for %s with query: %s" % (path, query_components))

        try:
            if path == ROUTE_INDEX:
                self.send_response(200)
                self.send_header('Content-type', 'text/html')
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
        path, _, _ = self.path.partition('?')

        print(u"[START]: Received POST for %s" % path)

        try:
            fs = cgi.FieldStorage(
                fp=self.rfile,
                headers=self.headers,
                environ={'REQUEST_METHOD': 'POST',
                         'CONTENT_TYPE': self.headers['Content-Type'],
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
            elif path == ROUTE_PRIVATE_SUB:
                r = self.subprocess_msg(form)
                self.send_json_response(r)

        except HTTPStatusError as err:
            self.send_error(err.code, err.message)

        print("[END]")

        return

    # Clean resources
    def shutdown(self):
        print("Web server is shutting down...")

        for key in self.clients.keys():
            client = self.clients[key]
            if client.process is not None:
                os.kill(client.process.pid, signal.SIGINT)
                client.process.terminate()
            if client.log_handler is not None:
                client.log_handler.close()
        self.clients.clear()

    # Send a JSON Response.
    def send_json_response(self, json_dict):
        self.send_response(200)
        self.send_header('Content-type', 'application/json')
        self.end_headers()
        obj = json.dumps(json_dict, indent=4)
        self.wfile.write(obj.encode(encoding='utf_8'))
        print("Send response: ", obj)
        return

    @staticmethod
    def check_mic(mic):
        if platform.system() == 'Darwin':
            result = subprocess.run(['ffmpeg', '-f', 'avfoundation', '-list_devices', 'true', '-i', ''],
                                    capture_output=True, text=True)
        else:
            result = subprocess.run(['ffmpeg', '-f', 'dshow', '-list_devices', '1', '-i', 'dummy'],
                                    capture_output=True, text=True, encoding="utf-8")

        # print(result)
        if result.stderr is not None:
            if mic in result.stderr:
                return True
        if result.stdout is not None:
            if mic in result.stdout:
                return True
        return False

    # Common Response
    @staticmethod
    def json_response(success, code, data):
        # 满足Windows客户端需求，进行修改
        if success:
            state = 1
        else:
            state = code
        return {"state": state, "code": data}

    @staticmethod
    def file_logger(identify):
        time_str = str(int(time.time()))
        log_path = os.path.join(ROOT, 'log')
        Path(log_path).mkdir(parents=True, exist_ok=True)
        log_file_path = os.path.join(log_path, '{id}_{t}.txt'.format(id=identify, t=time_str))
        print("---------- Log enabled file at: ", log_file_path)
        log = open(log_file_path, 'w', 1)
        return log

    @staticmethod
    def launch_janus(rtsp, room, display, identify, mic, client, debug, janus_signaling='ws://127.0.0.1:8188'):
        dir_path = os.path.dirname(os.path.realpath(__file__))
        janus_path = dir_path + "/janus.py"
        if platform.system() == "Windows":
            python = "python"
        else:
            python = "python3"
        cmd = [python, janus_path,
            janus_signaling,
            '--rtsp', rtsp,
            '--name', display,
            '--room', room,
            '--id', identify,
            '--mic', mic]
        if debug > 0:
            cmd.append("-v")
            if platform.system() == "Windows":
                p = subprocess.Popen(cmd, stdin=subprocess.PIPE, shell=True, text=True, encoding="utf-8")
            else:
                # write to file
                log = file_logger(identify)
                p = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=log, stderr=log)
                client.log_handler = log
        else:
            p = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)

        return p

    # check start command 
    def check_start(self, form):
        if 'debug' in form:
            debug = form['debug']
        if debug.isdigit():
            self.debug_log_level = int(debug)

        if 'room' not in form:
            return json_response(False, -1, "Please input Room number!")
        room = form["room"]
        if not room.isdigit():
            return json_response(False, -1, "Please input correct Room number!")

        if 'id' not in form:
            return json_response(False, -2, "Please input publisher ids to record!")
        publisher = str(form["id"])
        if not publisher.isdigit():
            return json_response(False, -2, "Please input correct publisher identifier!")

        if 'display' not in form:
            return json_response(False, -3, "Please input publisher display name in Janus room!")
        display = form["display"]

        if 'mic' not in form:
            return json_response(False, -4, "Please select a microphone device!")
        mic = form["mic"]
        if platform.system() == "Windows":
            if len(str(mic)) == 0:
                return json_response(False, -4, "Invalid microphone device!")
            if not check_mic(mic):
                return json_response(False, -4, "Invalid microphone device!")

        if 'janus' not in form:
            return json_response(False, -5, "Please input legal janus server address!")
        janus = form["janus"]

        if 'rtsp' not in form:
            return json_response(False, -5, "Please input RTSP stream to publish!")
        rtsp = form["rtsp"]
        if len(rtsp) == 0:
            return json_response(False, -2, "Please input correct RTSP address!")

        if rtsp in self.clients:
            print("Current RTSP stream ", rtsp, " is publishing...")
            return json_response(False, -3, "You've published the stream!")
        else:
            client = RTSPClient(publisher=publisher, rtsp=rtsp)
            proc = launch_janus(rtsp, room, display, publisher, mic, client, debug, janus)
            client.process = proc
            msg = rtsp + " has been published to VideoRoom " + room
            self.clients[rtsp] = client

            # Set a timeout 20s
            timeout = time.time() + 20
            while True:
                if time.time() > timeout:
                    return json_response(False, -9, 'Request subprocess timeout...')

                time.sleep(1)
                if not client.queue.empty():
                    obj = client.queue.get()
                    event = obj['event']
                    if event == 'close':
                        msg = str(obj['data'])
                        break
                    if event == 'ice':
                        data = str(obj['data'])
                        if data == 'completed':
                            break

            return json_response(True, 1, msg)

    # Check stop command
    def check_stop(self, form):
        if 'rtsp' not in form:
            return json_response(False, -1, "Please input RTSP stream to publish!")
        rtsp = form["rtsp"]
        if len(rtsp) == 0:
            return json_response(False, -2, "Please input correct RTSP address!")

        if rtsp not in self.clients:
            return json_response(False, -3, "No RTSP stream published!")

        client: RTSPClient = self.clients[rtsp]
        if client is not None and client.process is not None:
            print("Stopping SubProcess first!")
            os.kill(client.process.pid, signal.SIGINT)
            client.process.terminate()
            self.clients.pop(rtsp, None)
            msg = rtsp + " Stopped!"

            if client.log_handler is not None:
                client.log_handler.flush()
                client.log_handler.close()

            return json_response(True, 1, msg)

        return json_response(False, -4, "No subproc Found!")

    # Interact with subprocess
    def subprocess_msg(self, form):
        print("Received message: ", form)
        if 'rtsp' in form:
            rtsp = form['rtsp']
            if rtsp in self.clients:
                client = self.clients[rtsp]
                client.queue.put(form)

        return json_response(True, 1, "")



# common response
def json_response(success, code, data):
    # 满足Windows客户端需求，进行修改
    if success:
        state = 1
    else:
        state = code

    print("Send response: ", data)
    print("[END] \n")
    return web.json_response({"state": state, "code": data})


# index
async def index(request):
    content = "Recording the conference!"
    return web.Response(content_type="text/html", text=content)


async def subprocess_msg(request):
    print(u"[START] :Incoming Internal Request: {r}".format(r=request))
    form = await request.post()
    print("Received message: ", form)
    if 'rtsp' in form:
        rtsp = form['rtsp']
        if rtsp in CAMS:
            client = CAMS[rtsp]
            client.queue.put(form)

    return json_response(True, 1, "")


# check start command
async def start(request):
    print(u"[START] :Incoming Request: {r}".format(r=request))
    form = await request.post()
    print("form: ", form)

    if 'debug' in form:
        debug = form['debug']
        if debug.isdigit():
            global DEBUG_LEVEL
            DEBUG_LEVEL = int(debug)

    if 'room' not in form:
        return json_response(False, -1, "Please input Room number!")
    room = form["room"]
    if not room.isdigit():
        return json_response(False, -1, "Please input correct Room number!")

    if 'id' not in form:
        return json_response(False, -2, "Please input publisher ids to record!")
    publisher = str(form["id"])
    if not publisher.isdigit():
        return json_response(False, -2, "Please input correct publisher identifier!")

    if 'display' not in form:
        return json_response(False, -3, "Please input publisher display name in Janus room!")
    display = form["display"]

    if 'mic' not in form:
        return json_response(False, -4, "Please select a microphone device!")
    mic = form["mic"]
    if platform.system() == "Windows":
        if len(str(mic)) == 0:
            return json_response(False, -4, "Invalid microphone device!")
        if not check_mic(mic):
            return json_response(False, -4, "Invalid microphone device!")

    if 'janus' not in form:
        return json_response(False, -5, "Please input legal janus server address!")
    janus = form["janus"]

    if 'rtsp' not in form:
        return json_response(False, -5, "Please input RTSP stream to publish!")
    rtsp = form["rtsp"]
    if len(rtsp) == 0:
        return json_response(False, -2, "Please input correct RTSP address!")

    if rtsp in CAMS:
        print("Current RTSP stream ", rtsp, " is publishing...")
        return json_response(False, -3, "You've published the stream!")
    else:
        client = RTSPClient(publisher=publisher, rtsp=rtsp)
        proc = launch_janus(rtsp, room, display, publisher, mic, client, janus)
        client.process = proc
        msg = rtsp + " has been published to VideoRoom " + room
        CAMS[rtsp] = client

        # Set a timeout 20s
        timeout = time.time() + 20
        while True:
            if time.time() > timeout:
                return json_response(False, -9, 'Request subprocess timeout...')

            time.sleep(1)
            if not client.queue.empty():
                obj = client.queue.get()
                event = obj['event']
                if event == 'close':
                    msg = str(obj['data'])
                    break
                if event == 'ice':
                    data = str(obj['data'])
                    if data == 'completed':
                        break

        return json_response(True, 1, msg)


def check_mic(mic):
    if platform.system() == 'Darwin':
        result = subprocess.run(['ffmpeg', '-f', 'avfoundation', '-list_devices', 'true', '-i', ''],
                                capture_output=True, text=True)
    else:
        result = subprocess.run(['ffmpeg', '-f', 'dshow', '-list_devices', '1', '-i', 'dummy'],
                                capture_output=True, text=True, encoding="utf-8")

    # print(result)
    if result.stderr is not None:
        if mic in result.stderr:
            return True
    if result.stdout is not None:
        if mic in result.stdout:
            return True
    return False


def launch_janus(rtsp, room, display, identify, mic, client, janus_signaling='ws://127.0.0.1:8188'):
    dir_path = os.path.dirname(os.path.realpath(__file__))
    janus_path = dir_path + "/janus.py"
    if platform.system() == "Windows":
        python = "python"
    else:
        python = "python3"
    cmd = [python, janus_path,
           janus_signaling,
           '--rtsp', rtsp,
           '--name', display,
           '--room', room,
           '--id', identify,
           '--mic', mic]
    if DEBUG_LEVEL > 0:
        cmd.append("-v")
        if platform.system() == "Windows":
            p = subprocess.Popen(cmd, stdin=subprocess.PIPE, shell=True, text=True, encoding="utf-8")
        else:
            # write to file
            log = file_logger(identify)
            p = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=log, stderr=log)
            client.log_handler = log
    else:
        p = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)

    return p


# check stop command
async def stop(request):
    print(u"[START] :Incoming Request: {r}".format(r=request))
    form = await request.post()
    print("form: ", form)

    if 'rtsp' not in form:
        return json_response(False, -1, "Please input RTSP stream to publish!")
    rtsp = form["rtsp"]
    if len(rtsp) == 0:
        return json_response(False, -2, "Please input correct RTSP address!")

    if rtsp not in CAMS:
        return json_response(False, -3, "No RTSP stream published!")

    client: RTSPClient = CAMS[rtsp]
    if client is not None and client.process is not None:
        print("Stopping SubProcess first!")
        os.kill(client.process.pid, signal.SIGINT)
        client.process.terminate()
        CAMS.pop(rtsp, None)
        msg = rtsp + " Stopped!"

        if client.log_handler is not None:
            client.log_handler.flush()
            client.log_handler.close()

        return json_response(True, 1, msg)

    return json_response(False, -4, "No subproc Found!")


def file_logger(identify):
    time_str = str(int(time.time()))
    log_path = os.path.join(ROOT, 'log')
    Path(log_path).mkdir(parents=True, exist_ok=True)
    log_file_path = os.path.join(log_path, '{id}_{t}.txt'.format(id=identify, t=time_str))
    print("---------- Log enabled file at: ", log_file_path)
    log = open(log_file_path, 'w', 1)
    return log


def __start_internal_server():
    print("Start internal socket server at 9009")
    address = ('localhost', 9009)
    listener = Listener(address, authkey=b'hello')
    thread_quit = threading.Event()
    thread = threading.Thread(
        name="Internal Server Thread",
        target=__internal_server_worker,
        args=(
            asyncio.get_event_loop(),
            thread_quit,
            listener
        ),
    )
    thread.start()
    return listener


def __internal_server_worker(loop, quit_event, listener):
    conn = listener.accept()
    print('connection accepted from', listener.last_accepted)
    while not quit_event.is_set():
        try:
            msg = conn.recv()
            print("Received message: ", msg)
            if 'rtsp' in msg:
                rtsp = msg['rtsp']
                if rtsp in CAMS:
                    client = CAMS[rtsp]
                    client.queue.put(msg)
            # do something with msg
            if msg == 'close':
                conn.close()
                break
        except Exception as exc:
            print(exc)
            listener.close()
            return


async def on_shutdown(app):
    print("Web server is shutting down...")

    for key in CAMS.keys():
        client = CAMS[key]
        if client.process is not None:
            os.kill(client.process.pid, signal.SIGINT)
            client.process.terminate()
        if client.log_handler is not None:
            client.log_handler.close()
    CAMS.clear()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="accrtsprtc")
    parser.add_argument(
        "--p",
        type=int,
        default=9001,
        help="HTTP port number, default is 9001",
    )
    parser.add_argument(
        "--host", default="0.0.0.0", help="Host for HTTP server (default: 0.0.0.0)"
    )
    args = parser.parse_args()
    # print('Started HTTP server on port', args.p)

    # app = web.Application()
    # app.on_shutdown.append(on_shutdown)
    # app.router.add_get("/", index)
    # app.router.add_post("/camera/push/start", start)
    # app.router.add_post("/camera/push/stop", stop)
    # app.router.add_post("/camera/subprocess", subprocess_msg)

    # try:
    #     print("RTSP push server started")
    #     web.run_app(
    #         app, access_log=None, host=args.host, port=args.p, handle_signals=True
    #     )
    # except KeyboardInterrupt:
    #     pass
    # finally:
    #     print("Stopping now!")

    server = HTTPServer(('', args.p), RequestHandler)
    print('Started httpserver on port', args.p)

    try:
        server.serve_forever()
    except (KeyboardInterrupt, Exception) as e:
        print("Received exception: ", e)
        pass
    finally:
        print("Stopping now!")
        server.shutdown()
        server.socket.close()

