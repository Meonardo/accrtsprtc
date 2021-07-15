
import cv2

from av.filter import Filter, Graph
from threading import Timer
from av import VideoFrame
from aiortc.contrib.media import MediaPlayer, MediaRecorder, MediaRelay
from aiortc.mediastreams import MediaStreamTrack


def link_nodes(*nodes):
    for c, n in zip(nodes, nodes[1:]):
        c.link_to(n)

class VideoTransformTrack(MediaStreamTrack):
    kind = "video"

    def __init__(self, track:MediaStreamTrack, track2:MediaStreamTrack, transform):
        super().__init__()  # don't forget this!

        self.track = track
        self.transform = transform
        self.graph = Graph()
        link_nodes(
            self.graph.add_buffer(template=ivstrm),
            self.graph.add("scale", "iw/2:ih/2"),
            self.graph.add('buffersink')
        )
        self.graph.configure()

        print("Video Transform Begins!")

    async def recv(self):
        frame = await self.track.recv()
        frame2 = await self.track2.recv()
        print("Transforming...")

        if self.transform == "overlay":
            # rotate image
            img = frame.to_ndarray(format="bgr24")
            img2 = frame2.to_ndarray(format="bgr24")


            # rows, cols, _ = img.shape
            # M = cv2.getRotationMatrix2D((cols / 2, rows / 2), frame.time * 45, 1)
            # img = cv2.warpAffine(img, M, (cols, rows))

            # # rebuild a VideoFrame, preserving timing information

            
            new_frame = VideoFrame.from_ndarray(img, format="bgr24")
            new_frame.pts = frame.pts
            new_frame.time_base = frame.time_base
            return new_frame
        else:
            return frame