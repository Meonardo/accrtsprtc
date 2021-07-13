from av import AudioFrame
from pydub import AudioSegment
import pyaudio
import av

from aiortc.mediastreams import MediaStreamTrack

class RadioTelephoneTrack(MediaStreamTrack):
	kind = "audio"
	
	def __init__(self):
		super().__init__()  # don't forget this!
		
		self.sample_rate = 8000
		self.AUDIO_PTIME = 0.020  # 20ms audio packetization
		self.samples = int(self.AUDIO_PTIME * self.sample_rate)

		self.FORMAT = pyaudio.paInt16
		self.CHANNELS = 2
		self.RATE = self.sample_rate
		#self.RATE = 44100
		self.CHUNK = int(8000*0.020)
		#self.CHUNK = 1024
		
		self.p = pyaudio.PyAudio()
		self.mic_stream = self.p.open(format=self.FORMAT, channels=1,rate=self.RATE, input=True,frames_per_buffer=self.CHUNK)
		
		self.codec = av.CodecContext.create('pcm_s16le', 'r')
		self.codec.sample_rate = self.RATE
		#self.codec.sample_fmt = AV_SAMPLE_FMT_S16
		self.codec.channels = 2
		#self.codec.channel_layout = "mono";
		
		self.sound1 = AudioSegment.from_file(r"ΑΓΙΑ ΣΚΕΠΗ.mp3").set_frame_rate(self.sample_rate)
		print("Frame rate: "+str(self.sound1.frame_rate))
		#self.sound1_channels = self.sound1.split_to_mono()
		#self.sound1 = self.sound1_channels[0].overlay(self.sound1_channels[1])
		self.audio_samples = 0
		self.chunk_number = 0
		#self.sound1 = self.sound1 - 30 # make sound1 quiter 30dB
		
	async def recv(self):
		mic_data = self.mic_stream.read(self.CHUNK)
		mic_sound = AudioSegment(mic_data, sample_width=2, channels=1, frame_rate=self.RATE)
		mic_sound = AudioSegment.from_mono_audiosegments(mic_sound, mic_sound)
		mic_sound_duration = len(mic_sound)
		#print("Mic sound duration: "+str(mic_sound_duration))
		
		mp3_slice_duration = mic_sound_duration
		
		if(len(self.sound1)>(self.chunk_number+1)*mp3_slice_duration):
			sound1_part = self.sound1[self.chunk_number*mp3_slice_duration:(self.chunk_number+1)*mp3_slice_duration]
		elif(len(self.sound1)>(self.chunk_number)*mp3_slice_duration):
			sound1_part = self.sound1[self.chunk_number*mp3_slice_duration:]
		else:
			#replay
			
			times_played_1 = int((self.chunk_number)*mp3_slice_duration/len(self.sound1))
			times_played_2 = int((self.chunk_number+1)*mp3_slice_duration/len(self.sound1))
			if(times_played_1==times_played_2):
				time_start = ((self.chunk_number)*mp3_slice_duration)-(times_played_1*len(self.sound1))
				time_end = ((self.chunk_number+1)*mp3_slice_duration)-(times_played_1*len(self.sound1))
				sound1_part = self.sound1[time_start:time_end]
			else:
				time_start_1 = ((self.chunk_number)*mp3_slice_duration)-(times_played_1*len(self.sound1))
				sound1_part1 = self.sound1[time_start_1:]
				
				time_end_1 = ((self.chunk_number+1)*mp3_slice_duration)-(times_played_2*len(self.sound1))
				sound1_part2 = self.sound1[0:time_end_1]
				
				sound1_part = sound1_part1.append(sound1_part2, crossfade=5)
			
			#sound1_part = AudioSegment.silent()
			
		#self.mix_sound = sound1_part.overlay(mic_sound)
		
		
		self.mix_sound = sound1_part
		
		packet = av.Packet(self.mix_sound.raw_data)
		frame = self.codec.decode(packet)[0]
		
		frame.pts = self.audio_samples
		self.audio_samples += frame.samples
		
		
		self.chunk_number = self.chunk_number+1
		return frame
		
class RadioOutputStream:
	def __init__(self):
		self.FORMAT = pyaudio.paInt16
		self.CHANNELS = 1
		self.RATE = 44100
		self.CHUNK = 1024

		self.format_dtypes = {'dbl': '<f8','dblp': '<f8','flt': '<f4','fltp': '<f4','s16': '<i2','s16p':'<i2','s32': '<i4','s32p': '<i4','u8': 'u1','u8p': 'u1'}
		
		self.p = pyaudio.PyAudio()
		self.player = self.p.open(format=self.FORMAT,channels=self.CHANNELS, rate=self.RATE, output=True,frames_per_buffer=self.CHUNK)
		self.mic_stream = self.p.open(format=self.FORMAT, channels=self.CHANNELS,rate=self.RATE, input=True,frames_per_buffer=self.CHUNK)
		
		
		self.sound1 = AudioSegment.from_file(r"ΑΓΙΑ ΣΚΕΠΗ.mp3")
		self.sound1_channels = self.sound1.split_to_mono()
		self.sound1 = self.sound1_channels[0].overlay(self.sound1_channels[1])
		#self.sound1 = self.sound1 - 30 # make sound1 quiter 30dB

		self.sound2 = AudioSegment.from_file(r"ΑΓΙΑ ΚΥΡΙΑΚΗ.mp3")
		self.sound2_channels = self.sound2.split_to_mono()
		self.sound2 = self.sound2_channels[0].overlay(self.sound2_channels[1])
		self.sound2 = self.sound2 - 30 # make sound2 quiter 30dB

		self.sound3 = AudioSegment.from_file(r"ΑΓΙΟΙ ΑΓΓΕΛΟΙ.mp3")
		self.sound3_channels = self.sound3.split_to_mono()
		self.sound3 = self.sound3_channels[0].overlay(self.sound3_channels[1])
		self.sound3 = self.sound3 - 30 # make sound2 quiter 30dB
		
		self.codec = av.CodecContext.create('pcm_s16le', 'r')
		self.codec.sample_rate = self.RATE
		#self.codec.sample_fmt = AV_SAMPLE_FMT_S16
		self.codec.channels = 1;
		#self.codec.channel_layout = "mono";
		
		self.audio_samples = 0
		
		self.audio_sample_rate = 44100
		
		#self.audio_frames = []
		
		self.chunk_time = 24
		
		self.pieces = AudioSegment.silent()
		
	def run_stream(self):
		chunk_number = 0
		while(True):
			mic_data = self.mic_stream.read(self.CHUNK)
			mic_sound = AudioSegment(mic_data, sample_width=2, channels=1, frame_rate=self.RATE)
			mic_sound_duration = len(mic_sound)
			
			if(len(self.sound1)>(chunk_number+1)*mic_sound_duration):
				if(chunk_number==0):
					print(chunk_number*mic_sound_duration)
					print((chunk_number+1)*mic_sound_duration)
				sound1_part = self.sound1[chunk_number*mic_sound_duration:(chunk_number+1)*mic_sound_duration]
			elif(len(self.sound1)>(chunk_number)*mic_sound_duration):
				sound1_part = self.sound1[chunk_number*mic_sound_duration:]
			else:
				#replay
				
				times_played = int((chunk_number+1)*mic_sound_duration/len(self.sound1))
				time_start = ((chunk_number)*mic_sound_duration)-(times_played*len(self.sound1))
				time_end = ((chunk_number+1)*mic_sound_duration)-(times_played*len(self.sound1))
				sound1_part = self.sound1[time_start:time_end]
				
				#sound1_part = AudioSegment.silent()
			
			
			if(len(self.sound2)>(chunk_number+1)*mic_sound_duration):
				sound2_part = self.sound2[chunk_number*mic_sound_duration:(chunk_number+1)*mic_sound_duration]
			elif(len(self.sound2)>(chunk_number)*mic_sound_duration):
				sound2_part = self.sound2[chunk_number*mic_sound_duration:]
			else:
				sound2_part = AudioSegment.silent()
				#print("Silent")
				
			if(len(self.sound3)>(chunk_number+1)*mic_sound_duration):
				sound3_part = self.sound3[chunk_number*mic_sound_duration:(chunk_number+1)*mic_sound_duration]
			elif(len(self.sound3)>(chunk_number)*mic_sound_duration):
				sound3_part = self.sound3[chunk_number*mic_sound_duration:]
			else:
				sound3_part = AudioSegment.silent()
				#print("Silent")
		
			
			
			#self.mix_sound = sound1_part.overlay(sound2_part).overlay(sound3_part).overlay(mic_sound)
			#self.mix_sound = sound1_part.overlay(sound2_part).overlay(sound3_part)
			#self.mix_sound = sound1_part.overlay(mic_sound)
			#self.mix_sound = sound1_part.set_sample_width(4).set_channels(2) #error
			self.mix_sound = sound1_part
			#self.player.write(self.mix_sound.raw_data)	#high quality
			
			
			packet = av.Packet(self.mix_sound.raw_data)
			self.audio_frame = self.codec.decode(packet)[0]
			
			#self.audio_frames.append(self.audio_frame)
			
			self.audio_frame.pts = self.audio_samples
			#print(self.audio_frame.pts)
			#self.audio_frame.time_base = fractions.Fraction(1, self.audio_sample_rate)
			self.audio_samples += self.audio_frame.samples
			
			#print(self.audio_frame)
			
			#code if i want to play AV AudioFrame
			
			'''
			for p in self.audio_frame.planes:
				data = p.to_bytes()
				data_segment = AudioSegment(data, sample_width=2, channels=1, frame_rate=44100)
				self.pieces = self.pieces+data_segment
				#low quality
				#self.player.write(data_segment.raw_data)
			'''
			
			chunk_number = chunk_number+1
			
			if(chunk_number % 500 == 0):
				self.pieces.export(str(chunk_number)+".mp3", format="mp3")
				self.pieces = AudioSegment.silent()
			
			

#radio_stream = RadioOutputStream()
#radio_stream.run_stream()
