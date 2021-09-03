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
from threading import Thread

CAMS = {}
# debug level 0 means nothing, 1 means debug
LOGS = {}
DEBUG_LEVEL = 1
STOP_FLUSH_LOG = False
ROOT = os.path.abspath(os.path.dirname(__file__))


def async_func(f):
    def wrapper(*args, **kwargs):
        thr = Thread(target = f, args = args, kwargs = kwargs)
        thr.start()
    return wrapper


# common response
def json_response(success, code, data):
    # 满足Windows客户端需求，进行修改
    if success:
        state = 1
    else:
        state = code

    print("Send response: ", data)
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
    time_str = datetime.datetime.now(datetime.timezone(datetime.timedelta(0))).astimezone().isoformat(sep=' ',
                                                                                                      timespec='milliseconds')
    print(u"[START] {time}\n:Incoming Request: {r}".format(time=time_str, r=request))
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
        proc = launch_janus(rtsp, room, display, publisher, mic, janus)
        msg = rtsp + " has been published to VideoRoom " + room
        CAMS[rtsp] = proc
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
        # write to file
        log = file_logger(identify)
        p = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=log, stderr=log)
        log_key = rtsp + '_log'
        LOGS[log_key] = log
    else:
        p = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        
    return p


# check stop command
async def stop(request):
    time_str = datetime.datetime.now(datetime.timezone(datetime.timedelta(0))).astimezone().isoformat(sep=' ',
                                                                                                      timespec='milliseconds')
    print(u"[START] {time}\n:Incoming Request: {r}".format(time=time_str, r=request))
    form = await request.post()
    print("form: ", form)

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

        log_key = rtsp + "_log"
        if log_key in LOGS:
            log = LOGS[log_key]
            if log is not None:
                log.flush()
                log.close()
                LOGS.pop(log_key, None)

        return json_response(True, 1, msg)

    return json_response(False, -4, "No subproc Found!")


@async_func
def check_io():
    if DEBUG_LEVEL == 0 or STOP_FLUSH_LOG:
        return
    while True:
        print("Writing log to file ", )
        time.sleep(3)
        for key in LOGS.keys():
            log_handle = LOGS[key]
            if log_handle is not None:
                log_handle.flush()


def file_logger(identify):
    time_str = str(int(time.time()))
    log_path = os.path.join(ROOT, 'log')
    Path(log_path).mkdir(parents=True, exist_ok=True)
    log_file_path = os.path.join(log_path, '{id}_{t}.txt'.format(id=identify, t=time_str))
    print("---------- Log enabed file at: ", log_file_path)
    log = open(log_file_path, 'w', 1)
    return log


async def on_shutdown(app):
    print("Web server is shutting down...")
    global STOP_FLUSH_LOG
    STOP_FLUSH_LOG = True

    for key in CAMS.keys():
        proc = CAMS[key]
        if proc is not None:
            os.kill(proc.pid, signal.SIGINT)
            proc.terminate()
    CAMS.clear()
    for key in LOGS.keys():
        log_handle = LOGS[key]
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

    if platform.system() == "Windows":
        check_io()

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

