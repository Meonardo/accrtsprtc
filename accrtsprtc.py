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

from pathlib import Path
from janus import print
from collections import namedtuple
from http.server import BaseHTTPRequestHandler, HTTPServer
from socketserver import ThreadingMixIn


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


class ThreadedHTTPServer(ThreadingMixIn, HTTPServer):
    """Handle requests in a separate thread."""


class RTSPClient:
    def __init__(self, room, publisher, rtsp, display, mic):
        self.room = room
        self.publisher = publisher
        self.rtsp = rtsp
        self.display = display
        self.process = None
        self.log_handler = None
        self.request_session = None
        self.mic = mic

        self.turn = "turn:192.168.5.233:3478"
        self.turn_user = "root"
        self.turn_passwd = "123456"

        self.stun = 'stun'

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

        print(u"[START]\n"
              u"Received GET for %s with query: %s" % (path, query_components))

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

        print("[END]\n")

        return

    # Handler for the POST requests
    def do_POST(self):
        path, _, _ = self.path.partition('?')

        print(u"[START]\n"
              u"Received POST for %s" % path)

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

        print("[END]\n")

        return

    # Clean resources
    def shutdown(self):
        print("Web server is shutting down...")

        for key in self.clients.keys():
            client = self.clients[key]
            self.kill_subprocess(client)
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

    def launch_janus(self, client, janus_signaling='ws://127.0.0.1:8188'):
        dir_path = os.path.dirname(os.path.realpath(__file__))
        janus_path = dir_path + "/janus.py"
        if platform.system() == "Windows":
            python = "python"
        else:
            python = "python3"
        cmd = [python, janus_path, janus_signaling, '--rtsp', client.rtsp, '--name', client.display, '--room',
               client.room, '--id', client.publisher, '--mic', client.mic]

        if client.turn is not None and client.turn_user is not None and client.turn_passwd is not None:
            cmd.extend(['--turn', client.turn, '--turn_user', client.turn_user, '--turn_passwd', client.turn_passwd,
                        '--stun', client.stun])

        if self.debug_log_level > 0:
            cmd.extend(['-L', str(self.debug_log_level)])
            log = self.file_logger(client.publisher)
            p = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=log, stderr=log, bufsize=1,
                                 universal_newlines=True, encoding="utf-8")
            client.log_handler = log
        else:
            p = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)

        return p

    # check start command
    def check_start(self, form):
        self.debug_log_level = 0
        if 'debug' in form:
            debug = form['debug']
            if debug.isdigit():
                self.debug_log_level = int(debug)

        if 'room' not in form:
            return self.json_response(False, -1, "Please input Room number!")
        room = form["room"]
        if not room.isdigit():
            return self.json_response(False, -1, "Please input correct Room number!")

        if 'id' not in form:
            return self.json_response(False, -2, "Please input publisher ids to record!")
        publisher = str(form["id"])
        if not publisher.isdigit():
            return self.json_response(False, -2, "Please input correct publisher identifier!")

        if 'display' not in form:
            return self.json_response(False, -3, "Please input publisher display name in Janus room!")
        display = form["display"]

        mic = 'mute'
        if 'mic' in form:
            mic = form["mic"]
            if platform.system() == "Windows":
                if len(str(mic)) == 0:
                    mic = "mute"
                if str(mic) != 'mute':
                    if not self.check_mic(mic):
                        return self.json_response(False, -4, "Invalid microphone device!")

        turn_server = None
        turn_passwd = None
        turn_user = None
        stun_server = None
        if 'turn_server' in form and 'turn_passwd' in form and 'turn_user' in form:
            turn_server = str(form['turn_server'])
            turn_passwd = str(form['turn_passwd'])
            turn_user = str(form['turn_user'])
            if not turn_server.startswith('turn'):
                return self.json_response(False, -4, "Invalid TURN server address!")
        if 'stun_server' in form:
            if not turn_server.startswith('stun'):
                return self.json_response(False, -4, "Invalid STUN server address!")

        if 'janus' not in form:
            return self.json_response(False, -5, "Please input legal janus server address!")
        janus = form["janus"]

        if 'rtsp' not in form:
            return self.json_response(False, -5, "Please input RTSP stream to publish!")
        rtsp = form["rtsp"]
        if len(rtsp) == 0:
            return self.json_response(False, -2, "Please input correct RTSP address!")

        if rtsp in self.clients:
            print("Current RTSP stream ", rtsp, " is publishing...")
            return self.json_response(False, -3, "You've published the stream!")
        else:
            client = RTSPClient(publisher=publisher, rtsp=rtsp, mic=mic, display=display, room=room)
            if turn_passwd and turn_user and turn_server:
                client.turn_server = turn_server
                client.turn_user = turn_user
                client.turn_passwd = turn_passwd
            if stun_server:
                client.stun_server = stun_server

            proc = self.launch_janus(client, janus)
            client.process = proc
            msg = publisher + " has been published to VideoRoom " + room
            self.clients[publisher] = client

            # Set a timeout 20s
            timeout = time.time() + 40
            while True:
                if time.time() > timeout:
                    return self.json_response(False, -9, 'Request subprocess timeout...')

                time.sleep(1)
                if not client.queue.empty():
                    obj = client.queue.get()
                    event = obj['event']
                    if event == 'close':
                        msg = str(obj['data'])
                        break
                    if event == 'webrtc':
                        data = str(obj['data'])
                        if data == 'up':
                            break
                    elif event == 'exception':
                        msg = str(obj['data'])
                        return self.json_response(False, -7, msg)

            return self.json_response(True, 1, msg)

    # Check stop command
    def check_stop(self, form):
        if 'id' not in form:
            return self.json_response(False, -1, "Please input correct Publisher ID!")
        publisher = str(form["id"])
        if len(publisher) == 0:
            return self.json_response(False, -2, "Please input correct Publisher ID!")

        if publisher not in self.clients:
            return self.json_response(False, -3, "No publisher {} currently publishing!".format(publisher))

        client: RTSPClient = self.clients[publisher]
        if client is not None:
            print("Stopping Subprocess first!")
            self.kill_subprocess(client)
            self.clients.pop(publisher, None)
            msg = "Publisher ID: " + publisher + " Stopped!"

            if client.log_handler is not None:
                client.log_handler.flush()
                client.log_handler.close()

            return self.json_response(True, 1, msg)

        return self.json_response(False, -4, "No subprocess Found!")

    @staticmethod
    def kill_subprocess(client: RTSPClient):
        if client.process is None:
            return
        try:
            os.kill(client.process.pid, signal.SIGINT)
            client.process.terminate()
        except Exception as e:
            print("Kill subprocess exception: ", e)

    # Interact with subprocess
    def subprocess_msg(self, form):
        print("Received message: ", form)
        if 'id' in form:
            publisher = str(form['id'])
            if publisher in self.clients:
                client = self.clients[publisher]
                client.queue.put(form)
                event = form['event']
                if event == 'exception':
                    self.kill_subprocess(client)

        return self.json_response(True, 1, "")


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

    server = ThreadedHTTPServer(('', args.p), RequestHandler)
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

