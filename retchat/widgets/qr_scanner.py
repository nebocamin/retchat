"""Camera view that reads QR codes.

The camera is opened through the XDG camera portal (asks for permission,
works in the Flatpak sandbox and with libcamera devices on phones via
PipeWire); without the portal, a V4L2 device is opened directly. Frames are
shown with gtk4paintablesink; at most SCAN_INTERVAL apart, a grayscale copy
is decoded with zxing-cpp on GStreamer's streaming thread.
"""

import glob
import os
import secrets
import threading
import time
from typing import Callable, List, Optional, Tuple

import gi
gi.require_version('Gtk', '4.0')
gi.require_version('Gst', '1.0')
gi.require_version('GstVideo', '1.0')
from gi.repository import Gio, GLib, Gst, GstVideo, Gtk

from retchat.qr import decode_gray

# Seconds between decoded frames (decoding takes ~10 ms per 720p frame)
SCAN_INTERVAL = 0.25
# Smaller frames are cheaper to convert and decode; QR codes held up to a
# camera are large enough at this size.
SIZE_CAPS = "video/x-raw,width=[1,1280],height=[1,1280]"

PORTAL = "org.freedesktop.portal.Desktop"
PORTAL_PATH = "/org/freedesktop/portal/desktop"
CAMERA_IFACE = "org.freedesktop.portal.Camera"


def _video_devices() -> List[str]:
    return sorted(glob.glob("/dev/video*"))


class QrScanner(Gtk.Box):
    """Shows the camera and calls ``on_found(text)`` for every QR code read.

    ``source`` replaces the camera with a GStreamer source description
    (tests). Call stop() when the scanner is no longer shown: it releases
    the camera.
    """

    def __init__(self, on_found: Callable[[str], None], source: Optional[str] = None):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        self._on_found = on_found
        self._source_override = source
        self._pipeline: Optional[Gst.Pipeline] = None
        self._bus_watch = 0
        self._portal_fd: Optional[int] = None
        self._attempts: List[Tuple[str, bool]] = []
        self._generation = 0  # invalidates callbacks of stopped sessions
        self._got_frame = False
        self._last_scan = 0.0
        self._scan_lock = threading.Lock()

        self.picture = Gtk.Picture(content_fit=Gtk.ContentFit.COVER, hexpand=True, vexpand=True)
        self.picture.set_size_request(240, 240)
        self.picture.add_css_class("qr-scanner-view")
        self.picture.set_overflow(Gtk.Overflow.HIDDEN)
        self.append(self.picture)

        self.status = Gtk.Label(wrap=True, justify=Gtk.Justification.CENTER)
        self.status.add_css_class("dim-label")
        self.append(self.status)

    # --- Public API -----------------------------------------------------------

    @property
    def running(self) -> bool:
        return self._pipeline is not None

    def start(self):
        if self._pipeline is not None:
            return
        self._generation += 1
        self._got_frame = False
        if not Gst.is_initialized():
            Gst.init(None)
        if self._source_override is not None:
            self._attempts = [(self._source_override, False)]
            self._next_attempt()
            return
        self._set_status("Kamera wird gestartet…")
        self._request_portal_camera(self._generation)

    def stop(self):
        self._generation += 1
        self._attempts = []
        self._stop_pipeline()
        if self._portal_fd is not None:
            os.close(self._portal_fd)
            self._portal_fd = None
        self.picture.set_paintable(None)

    # --- Camera access ----------------------------------------------------------

    def _request_portal_camera(self, generation: int):
        try:
            bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
            present = bus.call_sync(PORTAL, PORTAL_PATH, "org.freedesktop.DBus.Properties", "Get",
                                    GLib.Variant("(ss)", (CAMERA_IFACE, "IsCameraPresent")),
                                    GLib.VariantType("(v)"), Gio.DBusCallFlags.NONE, 3000, None)
        except GLib.Error:
            self._use_v4l2("Kein Kamera-Portal verfügbar")
            return
        if not present.unpack()[0]:
            self._use_v4l2("Das Kamera-Portal meldet keine Kamera")
            return

        # The request object path is known in advance; subscribe before asking,
        # so a fast answer isn't missed.
        token = f"retchat_{secrets.token_hex(4)}"
        sender = bus.get_unique_name()[1:].replace(".", "_")
        request_path = f"{PORTAL_PATH}/request/{sender}/{token}"
        subscription = 0

        def on_response(_bus, _sender, _path, _iface, _signal, params):
            bus.signal_unsubscribe(subscription)
            if generation != self._generation:
                return
            response, _results = params.unpack()
            if response == 0:
                self._open_portal_remote(bus, generation)
            elif response == 1:
                self._set_status("Kamerazugriff abgelehnt. Du kannst stattdessen ein Bild laden.")
            else:
                self._set_status("Kamerazugriff nicht möglich. Du kannst stattdessen ein Bild laden.")

        subscription = bus.signal_subscribe(PORTAL, "org.freedesktop.portal.Request", "Response",
                                            request_path, None, Gio.DBusSignalFlags.NONE, on_response)

        def on_access(_bus, result):
            try:
                bus.call_finish(result)
            except GLib.Error as e:
                bus.signal_unsubscribe(subscription)
                if generation == self._generation:
                    self._use_v4l2(f"Kamera-Portal: {e.message}")

        bus.call(PORTAL, PORTAL_PATH, CAMERA_IFACE, "AccessCamera",
                 GLib.Variant("(a{sv})", ({"handle_token": GLib.Variant("s", token)},)),
                 GLib.VariantType("(o)"), Gio.DBusCallFlags.NONE, -1, None, on_access)

    def _open_portal_remote(self, bus: Gio.DBusConnection, generation: int):
        def on_remote(_bus, result):
            try:
                value, fd_list = bus.call_with_unix_fd_list_finish(result)
                fd = fd_list.get(value.unpack()[0])
            except GLib.Error as e:
                if generation == self._generation:
                    self._use_v4l2(f"PipeWire: {e.message}")
                return
            if generation != self._generation:
                os.close(fd)
                return
            self._portal_fd = fd
            self._attempts = [(f"pipewiresrc fd={fd} client-name=Retchat", True),
                              (f"pipewiresrc fd={fd} client-name=Retchat", False)]
            self._attempts += self._v4l2_attempts()
            self._next_attempt()

        bus.call_with_unix_fd_list(PORTAL, PORTAL_PATH, CAMERA_IFACE, "OpenPipeWireRemote",
                                   GLib.Variant("(a{sv})", ({},)), GLib.VariantType("(h)"),
                                   Gio.DBusCallFlags.NONE, -1, None, None, on_remote)

    @staticmethod
    def _v4l2_attempts() -> List[Tuple[str, bool]]:
        if not _video_devices():
            return []
        return [("v4l2src", True), ("v4l2src", False)]

    def _use_v4l2(self, reason: str):
        self._attempts = self._v4l2_attempts()
        if not self._attempts:
            self._set_status(f"Keine Kamera gefunden ({reason}). Du kannst stattdessen ein Bild laden.")
            return
        self._next_attempt()

    # --- Pipeline ---------------------------------------------------------------

    def _next_attempt(self):
        self._stop_pipeline()
        if not self._attempts:
            self._set_status("Die Kamera konnte nicht gestartet werden. Du kannst stattdessen ein Bild laden.")
            return
        source, limit_size = self._attempts.pop(0)
        size = f" ! {SIZE_CAPS}" if limit_size else ""
        description = (
            f"{source}{size} ! videoconvert ! tee name=t "
            "t. ! queue leaky=downstream max-size-buffers=2 ! videoconvert ! gtk4paintablesink name=view "
            "t. ! queue leaky=downstream max-size-buffers=1 ! videoconvert ! video/x-raw,format=GRAY8 "
            "! appsink name=frames max-buffers=1 drop=true sync=false emit-signals=true"
        )
        try:
            pipeline = Gst.parse_launch(description)
        except GLib.Error as e:
            self._set_status(f"Kamera: {e.message}")
            self._next_attempt()
            return
        self._pipeline = pipeline
        self.picture.set_paintable(pipeline.get_by_name("view").get_property("paintable"))
        generation = self._generation
        pipeline.get_by_name("frames").connect("new-sample", self._on_sample, generation)
        bus = pipeline.get_bus()
        bus.add_signal_watch()
        self._bus_watch = bus.connect("message", self._on_bus_message, generation)
        self._set_status("Richte die Kamera auf einen QR-Code")
        if pipeline.set_state(Gst.State.PLAYING) == Gst.StateChangeReturn.FAILURE:
            self._next_attempt()

    def _stop_pipeline(self):
        pipeline, self._pipeline = self._pipeline, None
        if pipeline is None:
            return
        bus = pipeline.get_bus()
        if self._bus_watch:
            bus.disconnect(self._bus_watch)
            self._bus_watch = 0
        bus.remove_signal_watch()
        pipeline.set_state(Gst.State.NULL)

    def _on_bus_message(self, _bus, message: Gst.Message, generation: int):
        if generation != self._generation:
            return
        if message.type == Gst.MessageType.ERROR:
            err, _debug = message.parse_error()
            if self._got_frame or not self._attempts:
                self._stop_pipeline()
                self._set_status(f"Kamera-Fehler: {err.message}. Du kannst stattdessen ein Bild laden.")
            else:
                # e.g. the size limit couldn't be negotiated: next variant
                self._next_attempt()

    def _on_sample(self, sink, generation: int) -> Gst.FlowReturn:
        """New grayscale frame (GStreamer streaming thread)."""
        sample = sink.emit("pull-sample")
        if sample is None:
            return Gst.FlowReturn.OK
        if not self._got_frame:
            self._got_frame = True
        now = time.monotonic()
        if now - self._last_scan < SCAN_INTERVAL or not self._scan_lock.acquire(blocking=False):
            return Gst.FlowReturn.OK
        try:
            self._last_scan = now
            info = GstVideo.VideoInfo.new_from_caps(sample.get_caps())
            buffer = sample.get_buffer()
            ok, mapping = buffer.map(Gst.MapFlags.READ)
            if not ok:
                return Gst.FlowReturn.OK
            try:
                data = bytes(mapping.data)
            finally:
                buffer.unmap(mapping)
            texts = decode_gray(data, info.width, info.height, info.stride[0])
        except Exception:
            texts = []
        finally:
            self._scan_lock.release()
        if texts:
            GLib.idle_add(self._deliver, texts, generation)
        return Gst.FlowReturn.OK

    def _deliver(self, texts: List[str], generation: int) -> bool:
        if generation == self._generation:
            for text in texts:
                self._on_found(text)
                if generation != self._generation:
                    break  # the handler stopped scanning
        return False

    def _set_status(self, text: str):
        self.status.set_text(text)
