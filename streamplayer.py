
import av
import threading
import asyncio
import errno
import time


class StreamPlayer (threading.Thread):
    def __init__(self, rtsp, loop=asyncio.get_event_loop()):
        threading.Thread.__init__(self)
        # flag to indicate that the thread should stop
        self.isRunning = False
        self.rtsp = rtsp
        self.packets = asyncio.Queue(1)
        self.name = "StreamPlayer--" + rtsp
        self.loop = loop

        options = {'rtsp_transport': 'tcp'}
        self.container = av.open(rtsp, mode="r", metadata_encoding='utf-8', options=options)

    def run(self):
        """
        start the thread until a stop is requested.
        :return:
        """
        print("starting player thread")
        self.isRunning = True
        video_stream = self.container.streams.video[0]

        while self.isRunning:
            if not self.isRunning:
                break
            try:
                packet = next(self.container.demux(video_stream))
                # print(self.debug_desc + " Original Decoded Frame: ", frame)
            except (av.AVError, BlockingIOError, StopIteration) as exc:
                if isinstance(exc, av.FFmpegError) and exc.errno == errno.EAGAIN:
                    time.sleep(0.01)
                    continue
                else:
                    break
            if not self.packets.full():
                asyncio.run_coroutine_threadsafe(self.packets.put(packet), self.loop)

        return

    def stop(self):
        if self.isRunning:
            self.isRunning = False
            self.container.close()
        print("H264 Streaming Player was shutdown!")
