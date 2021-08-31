#!/usr/bin/python3
import platform
import os
import argparse
import subprocess
import signal
import time
import datetime
from pathlib import Path
from aiohttp import web

CAMS = {}
# debug level 0 means nothing, 1 means debug
LOGS = {}
DEBUG_LEVEL = 0
ROOT = os.path.abspath(os.path.dirname(__file__))


# common response
def json_response(success, code, data):
    # 满足Windows客户端需求，进行修改
    if success:
        state = 1
    else:
        state = code

    time_str = datetime.datetime.now(datetime.timezone(datetime.timedelta(0))).astimezone().isoformat(sep=' ',
                                                                                           timespec='milliseconds')
    print("[END] {}\n".format(time_str))
    return web.json_response({"state": state, "code": data})


# index
async def index(request):
    content = "Recording the conference!"
    return web.Response(content_type="text/html", text=content)


# check start command
async def start(request):
    form = await request.post()
    time_str = datetime.datetime.now(datetime.timezone(datetime.timedelta(0))).astimezone().isoformat(sep=' ',
                                                                                           timespec='milliseconds')
    print(u"[START] {time}\n:Incoming Request: {r}, form: {f}".format(time=time_str, r=request, f=form))

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
        proc = launch_janus(rtsp, room, display, publisher, mic, janus)
        msg = rtsp + " has been published to VideoRoom " + room
        CAMS[rtsp] = proc
        return json_response(True, 1, msg)


def check_mic(mic):
    if platform.system() == 'Darwin':
        result = subprocess.run(['ffmpeg', '-f', 'avfoundation', '-list_devices', 'true', '-i', ''],
                                capture_output=True, text=True)
    else:
        result = subprocess.run(['ffmpeg', '-f', 'dshow', '-list_devices', 'true', '-i', 'dummy'],
                                capture_output=True, text=True)

    print(result.stderr)
    if mic in result.stdout or mic in result.stderr:
        return True
    return False


def launch_janus(rtsp, room, display, identify, mic, janus_signaling='ws://127.0.0.1:8188'):
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
        time_str = str(int(time.time()))
        log_path = os.path.join(ROOT, 'log')
        Path(log_path).mkdir(parents=True, exist_ok=True)
        log_file_path = os.path.join(log_path, '{id}_{t}.txt'.format(id=identify, t=time_str))
        print("---------- Log enabed file at: ", log_file_path)
        log = open(log_file_path, 'w')
        p = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=log)
        log_key = rtsp + '_log'
        LOGS[log_key] = log
    else:
        p = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE)
    return p


# check stop command
async def stop(request):
    form = await request.post()
    time_str = datetime.datetime.now(datetime.timezone(datetime.timedelta(0))).astimezone().isoformat(sep=' ',
                                                                                           timespec='milliseconds')
    print(u"[START] {time}\n:Incoming Request: {r}, form: {f}".format(time=time_str, r=request, f=form))

    if 'rtsp' not in form:
        return json_response(False, -1, "Please input RTSP stream to publish!")
    rtsp = form["rtsp"]
    if len(rtsp) == 0:
        return json_response(False, -2, "Please input correct RTSP address!")

    if rtsp not in CAMS:
        return json_response(False, -3, "No RTSP stream published!")

    proc: subprocess.Popen = CAMS[rtsp]
    if proc is not None:
        print("Stopping SubProcess first!")
        os.kill(proc.pid, signal.SIGINT)
        proc.terminate()
        CAMS.pop(rtsp, None)
        msg = rtsp + " Stopped!"
        return json_response(True, 1, msg)

    log_key = rtsp + "_log"
    if log_key in LOGS:
        log = LOGS[log_key]
        if log is not None:
            log.close()

    return json_response(False, -4, "No subproc Found!")


async def on_shutdown(app):
    print("Web server is shutting down...")

    for key in CAMS.keys():
        proc = CAMS[key]
        if proc is not None:
            os.kill(proc.pid, signal.SIGINT)
            proc.terminate()
    CAMS.clear()
    for key in LOGS.keys():
        log_handle = CAMS[key]
        if log_handle is not None:
            log_handle.close()
    LOGS.clear()


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
    print('Started HTTP server on port', args.p)

    app = web.Application()
    app.on_shutdown.append(on_shutdown)
    app.router.add_get("/", index)
    app.router.add_post("/camera/push/start", start)
    app.router.add_post("/camera/push/stop", stop)

    try:
        print("RTSP push server started at ", datetime.datetime.now(datetime.timezone(datetime.timedelta(0))).astimezone().isoformat(sep=' ',
                                                                                           timespec='milliseconds'))
        web.run_app(
            app, access_log=None, host=args.host, port=args.p
        )
    except KeyboardInterrupt:
        pass
    finally:
        print("Stopping now!")

