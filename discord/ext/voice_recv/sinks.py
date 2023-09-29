# -*- coding: utf-8 -*-

from __future__ import annotations

import abc
import time
import wave
import inspect
import audioop
import logging

from .opus import VoiceData
from .silence import SilenceGenerator

import discord

from discord.utils import MISSING
from discord.opus import Decoder as OpusDecoder

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from typing import Callable, Optional, Any, IO, Sequence, Tuple, Generator

    from .rtp import RTPPacket, RTCPPacket, FakePacket
    from .voice_client import VoiceRecvClient
    from .opus import VoiceData

    Packet = RTPPacket | FakePacket
    User = discord.User | discord.Member

    BasicSinkWriteCB = Callable[[Optional[User], VoiceData], Any]
    BasicSinkWriteRTCPCB = Callable[[RTCPPacket], Any]
    ConditionalFilterFn = Callable[[Optional[User], VoiceData], bool]


log = logging.getLogger(__name__)

__all__ = [
    'AudioSink',
    'MultiAudioSink',
    'BasicSink',
    'WaveSink',
    'PCMVolumeTransformer',
    'ConditionalFilter',
    'TimedFilter',
    'UserFilter',
    'SilenceGeneratorSink',
]


# TODO: use this in more places
class VoiceRecvException(discord.DiscordException):
    """Generic exception for voice recv related errors"""

    def __init__(self, message: str):
        self.message = message


class SinkMeta(abc.ABCMeta):
    __sink_listeners__: list[Tuple[str, str]]

    def __new__(cls, name: str, bases: Tuple[type, ...], attrs: dict[str, Any], **kwargs):
        listeners: dict[str, Any] = {}
        new_cls = super().__new__(cls, name, bases, attrs, **kwargs)

        for base in reversed(new_cls.__mro__):
            for elem, value in base.__dict__.items():
                # If it exists in a subclass, delete the higher level one
                if elem in listeners:
                    del listeners[elem]

                is_static_method = isinstance(value, staticmethod)
                if is_static_method:
                    value = value.__func__

                if not hasattr(value, '__sink_listener__'):
                    continue

                listeners[elem] = value

        listener_list = []
        for listener in listeners.values():
            for listener_name in listener.__sink_listener_names__:
                listener_list.append((listener_name, listener.__name__))

        new_cls.__sink_listeners__ = listener_list
        return new_cls


# TODO: replace AudioSink hints with a sink generic
class SinkABC(metaclass=SinkMeta):
    __sink_listeners__: list[Tuple[str, str]]

    @property
    @abc.abstractmethod
    def parent(self) -> Optional[AudioSink]:
        raise NotImplementedError

    @property
    @abc.abstractmethod
    def child(self) -> Optional[AudioSink]:
        raise NotImplementedError

    @property
    @abc.abstractmethod
    def children(self) -> Sequence[AudioSink]:
        raise NotImplementedError

    @property
    @abc.abstractmethod
    def voice_client(self) -> Optional[VoiceRecvClient]:
        raise NotImplementedError

    # TODO: handling opus vs pcm is not strictly mutually exclusive
    #       a sink could handle both but idk about that pattern
    @abc.abstractmethod
    def wants_opus(self) -> bool:
        """If sink handles opus data"""
        raise NotImplementedError

    @abc.abstractmethod
    def write(self, user: Optional[User], data: VoiceData):
        """Callback for when the sink receives data"""
        raise NotImplementedError

    @abc.abstractmethod
    def cleanup(self):
        raise NotImplementedError

    def walk_children(self) -> Generator[AudioSink, None, None]:
        for child in self.children:
            yield child
            yield from child.walk_children()


class AudioSink(SinkABC):
    _voice_client: Optional[VoiceRecvClient]
    _parent: Optional[AudioSink] = None
    _child: Optional[AudioSink]

    def __init__(self, destination: Optional[AudioSink] = None, /):
        self._child = destination

        if destination is not None:
            destination._parent = self

    def __del__(self):
        self.cleanup()

    @property
    def parent(self) -> Optional[AudioSink]:
        return self._parent

    @property
    def child(self) -> Optional[AudioSink]:
        return self._child

    @property
    def children(self) -> list[AudioSink]:
        return [self._child] if self._child else []

    @property
    def voice_client(self) -> Optional[VoiceRecvClient]:
        """Guaranteed to not be None inside write()"""

        if self.parent is not None:
            return self.parent.voice_client
        else:
            return self._voice_client

    @classmethod
    def listener(cls, name: str = MISSING):
        """Marks a function as an event listener."""

        if name is not MISSING and not isinstance(name, str):
            raise TypeError(f'AudioSink.listener expected str but received {type(name).__name__} instead.')

        def decorator(func):
            actual = func

            if isinstance(actual, staticmethod):
                actual = actual.__func__

            if inspect.iscoroutinefunction(actual):
                raise TypeError('Listener function must not be a coroutine function.')

            actual.__sink_listener__ = True
            to_assign = name or actual.__name__

            try:
                actual.__sink_listener_names__.append(to_assign)
            except AttributeError:
                actual.__sink_listener_names__ = [to_assign]

            return func

        return decorator


class MultiAudioSink(AudioSink):
    def __init__(self, destinations: Sequence[AudioSink], /):
        # Intentionally not calling super().__init__ here
        self._children = list(destinations)

        if destinations is not None:
            for dest in destinations:
                dest._parent = self

    @property
    def child(self) -> Optional[AudioSink]:
        return self._children[0] if self._children else None

    @property
    def children(self) -> list[AudioSink]:
        return self._children.copy()


class BasicSink(AudioSink):
    """Simple callback based sink."""

    def __init__(
        self,
        event: BasicSinkWriteCB,
        *,
        rtcp_event: Optional[BasicSinkWriteRTCPCB] = None,
        decode: bool = True,
    ):
        super().__init__()

        self.cb = event
        self.cb_rtcp = rtcp_event
        self.decode = decode

    def wants_opus(self) -> bool:
        return not self.decode

    def write(self, user: Optional[User], data: VoiceData):
        self.cb(user, data)

    @AudioSink.listener()
    def on_rtcp_packet(self, packet: RTCPPacket, guild: discord.Guild):
        self.cb_rtcp(packet) if self.cb_rtcp else None

    def cleanup(self):
        pass


class WaveSink(AudioSink):
    """Endpoint AudioSink that generates a wav file.
    Best used in conjunction with a silence generating sink. (TBD)
    """

    CHANNELS = OpusDecoder.CHANNELS
    SAMPLE_WIDTH = OpusDecoder.SAMPLE_SIZE // OpusDecoder.CHANNELS
    SAMPLING_RATE = OpusDecoder.SAMPLING_RATE

    def __init__(self, destination: str | IO[bytes]):
        super().__init__()

        self._file = wave.open(destination, 'wb')
        self._file.setnchannels(self.CHANNELS)
        self._file.setsampwidth(self.SAMPLE_WIDTH)
        self._file.setframerate(self.SAMPLING_RATE)

    def wants_opus(self) -> bool:
        return False

    def write(self, user: Optional[User], data: VoiceData):
        self._file.writeframes(data.pcm)

    def cleanup(self):
        try:
            self._file.close()
        except Exception:
            log.info("WaveSink got error closing file on cleanup", exc_info=True)


class PCMVolumeTransformer(AudioSink):
    """AudioSink used to change the volume of PCM data, just like
    :class:`discord.PCMVolumeTransformer`.
    """

    def __init__(self, destination: AudioSink, volume: float = 1.0):
        if not isinstance(destination, AudioSink):
            raise TypeError(f'expected AudioSink not {type(destination).__name__}')

        if destination.wants_opus():
            raise VoiceRecvException('AudioSink must not request Opus encoding.')

        super().__init__(destination)

        self.destination = destination
        self.volume = volume

    def wants_opus(self) -> bool:
        return False

    @property
    def volume(self) -> float:
        """Retrieves or sets the volume as a floating point percentage (e.g. 1.0 for 100%)."""
        return self._volume

    @volume.setter
    def volume(self, value: float):  # TODO: type range
        self._volume = max(value, 0.0)

    def write(self, user: Optional[User], data: VoiceData):
        data.pcm = audioop.mul(data.pcm, 2, min(self._volume, 2.0))
        self.destination.write(user, data)

    def cleanup(self):
        pass


class ConditionalFilter(AudioSink):
    """AudioSink for filtering packets based on an arbitrary predicate function."""

    def __init__(self, destination: AudioSink, predicate: ConditionalFilterFn):
        super().__init__(destination)

        self.destination = destination
        self.predicate = predicate

    def wants_opus(self) -> bool:
        return self.destination.wants_opus()

    def write(self, user: Optional[User], data: VoiceData):
        if self.predicate(user, data):
            self.destination.write(user, data)

    def cleanup(self):
        del self.predicate


class UserFilter(ConditionalFilter):
    """A convenience class for a User based ConditionalFilter."""

    def __init__(self, destination: AudioSink, user: User):
        super().__init__(destination, self._predicate)
        self.user = user

    def _predicate(self, user: Optional[User], data: VoiceData) -> bool:
        return user == self.user


class TimedFilter(ConditionalFilter):
    """A convenience class for a timed ConditionalFilter."""

    def __init__(self, destination: AudioSink, duration: int | float, *, start_on_init: bool = False):
        super().__init__(destination, self.predicate)
        self.duration = duration

        if start_on_init:
            self.start_time = self.get_time()
        else:
            self.start_time = None
            self.write = self._write_once

    def _write_once(self, user: Optional[User], data: VoiceData):
        self.start_time = self.get_time()
        super().write(user, data)
        self.write = super().write

    def predicate(self, user: Optional[User], data: VoiceData) -> bool:
        return self.start_time is not None and self.get_time() - self.start_time < self.duration

    def get_time(self) -> int | float:
        """Function to generate a timestamp.  Defaults to `time.perf_counter()`.
        Can be overridden.
        """
        return time.perf_counter()


class SilenceGeneratorSink(AudioSink):
    """Generates intermittent silence packets during transmission downtime."""

    def __init__(self, destination: AudioSink):
        super().__init__(destination)

        self.destination = destination
        self.silencegen = SilenceGenerator(self.destination.write)
        self.silencegen.start()

    def wants_opus(self) -> bool:
        return self.destination.wants_opus()

    def write(self, user: Optional[User], data: VoiceData):
        self.silencegen.push(user, data.packet)
        self.destination.write(user, data)

    @AudioSink.listener()
    def on_voice_member_disconnect(self, member: discord.Member, ssrc: int | None):
        self.silencegen.drop(ssrc=ssrc, user=member)

    def cleanup(self):
        self.silencegen.stop()

