
import av
import threading
import asyncio


class StreamPlayer (threading.Thread):
    def __init__(self, rtsp, loop=asyncio.get_event_loop()):
        threading.Thread.__init__(self)
        # flag to indicate that the thread should stop
        self.isRunning = False
        self.rtsp = rtsp
        self.packets = asyncio.Queue()
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

        while self.isRunning:
            if not self.isRunning:
                break
            for i, packet in enumerate(self.container.demux()):
                try:
                    if packet.dts is None:
                        continue

                    if packet.stream.type == 'video':
                        if not self.packets.full():
                            asyncio.run_coroutine_threadsafe(self.packets.put(packet), self.loop)
                            # self.packets.put(packet)

                except InterruptedError:
                    self.isRunning = False
                    break
        return

    def stop(self):
        if self.isRunning:
            self.isRunning = False
            self.container.close()
        print("H264 Streaming Player was shutdown!")
