"""Image viewer dialog/window for viewing full-size images received in chat."""

import os
import shutil
from typing import Optional

import gi
gi.require_version('Gtk', '4.0')
gi.require_version('Adw', '1')
from gi.repository import Gtk, Adw, Gdk, Gio, GLib


def format_file_size(size_bytes: int) -> str:
    if size_bytes < 1024:
        return f"{size_bytes} B"
    elif size_bytes < 1024 * 1024:
        return f"{size_bytes / 1024:.1f} KB"
    else:
        return f"{size_bytes / (1024 * 1024):.1f} MB"


class ImageViewerDialog(Adw.Window):
    def __init__(
        self,
        parent_window: Optional[Gtk.Window],
        image_path: str,
        image_name: Optional[str] = None,
        image_size: Optional[int] = None
    ):
        super().__init__()
        if parent_window:
            self.set_transient_for(parent_window)
            self.set_modal(True)

        self.image_path = image_path
        self.image_name = image_name or os.path.basename(image_path) or "image.jpg"
        if not self.image_name.lower().endswith(('.jpg', '.jpeg', '.png', '.webp', '.gif', '.bmp', '.svg')):
            self.image_name = f"{self.image_name}.jpg"

        file_size = image_size if image_size is not None else (os.path.getsize(image_path) if os.path.isfile(image_path) else 0)

        # Toolbar & Header
        toolbar_view = Adw.ToolbarView()
        self.set_content(toolbar_view)

        header_bar = Adw.HeaderBar()
        header_bar.add_css_class("flat")
        toolbar_view.add_top_bar(header_bar)

        # Open in external app button
        open_ext_btn = Gtk.Button(icon_name="document-open-symbolic")
        open_ext_btn.set_tooltip_text("In Standard-App öffnen")
        open_ext_btn.connect("clicked", self._on_open_external)
        header_bar.pack_start(open_ext_btn)

        # Save button
        save_btn = Gtk.Button(icon_name="document-save-symbolic")
        save_btn.set_tooltip_text("Bild speichern unter...")
        save_btn.add_css_class("suggested-action")
        save_btn.connect("clicked", self._on_save_clicked)
        header_bar.pack_end(save_btn)

        # Load texture to get resolution
        texture = None
        img_w, img_h = 0, 0
        try:
            gio_file = Gio.File.new_for_path(self.image_path)
            texture = Gdk.Texture.new_from_file(gio_file)
            img_w = texture.get_width()
            img_h = texture.get_height()
        except Exception as e:
            GLib.idle_add(lambda: print(f"Error loading image texture: {e}"))

        # Window Title
        subtitle = f"{format_file_size(file_size)}"
        if img_w > 0 and img_h > 0:
            subtitle = f"{img_w} × {img_h} • {subtitle}"

        window_title = Adw.WindowTitle(title=self.image_name, subtitle=subtitle)
        header_bar.set_title_widget(window_title)

        # Responsive window sizing
        win_w = min(960, max(360, img_w + 32)) if img_w > 0 else 600
        win_h = min(720, max(420, img_h + 80)) if img_h > 0 else 500
        self.set_default_size(win_w, win_h)

        # Scrolled Window content
        scrolled = Gtk.ScrolledWindow(vexpand=True, hexpand=True)
        scrolled.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)

        if texture:
            pic = Gtk.Picture.new_for_paintable(texture)
            pic.set_can_shrink(True)
            if hasattr(Gtk, "ContentFit") and hasattr(pic, "set_content_fit"):
                pic.set_content_fit(Gtk.ContentFit.SCALE_DOWN)
            elif hasattr(pic, "set_keep_aspect_ratio"):
                pic.set_keep_aspect_ratio(True)
            pic.set_halign(Gtk.Align.CENTER)
            pic.set_valign(Gtk.Align.CENTER)
            scrolled.set_child(pic)
        else:
            err_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
            err_box.set_halign(Gtk.Align.CENTER)
            err_box.set_valign(Gtk.Align.CENTER)
            icon = Gtk.Image.new_from_icon_name("image-missing-symbolic")
            icon.set_pixel_size(64)
            lbl = Gtk.Label(label="Bild konnte nicht geladen werden")
            err_box.append(icon)
            err_box.append(lbl)
            scrolled.set_child(err_box)

        toolbar_view.set_content(scrolled)

        # Escape key controller to close window
        key_controller = Gtk.EventControllerKey.new()
        key_controller.connect("key-pressed", self._on_key_pressed)
        self.add_controller(key_controller)

    def _on_key_pressed(self, _controller, keyval, _keycode, _state):
        if keyval == Gdk.KEY_Escape:
            self.close()
            return True
        return False

    def _on_open_external(self, _btn):
        try:
            Gio.AppInfo.launch_default_for_uri(f"file://{os.path.abspath(self.image_path)}", None)
        except Exception:
            import subprocess
            try:
                subprocess.Popen(["xdg-open", self.image_path])
            except Exception:
                pass

    def _on_save_clicked(self, _btn):
        if hasattr(Gtk, "FileDialog"):
            dialog = Gtk.FileDialog()
            dialog.set_title("Bild speichern unter")
            dialog.set_initial_name(self.image_name)

            def on_save_finish(d, result):
                try:
                    dest_file = d.save_finish(result)
                    if dest_file:
                        dest_path = dest_file.get_path()
                        if dest_path:
                            shutil.copyfile(self.image_path, dest_path)
                except Exception:
                    pass

            dialog.save(self, None, on_save_finish)
        else:
            fc = Gtk.FileChooserNative.new(
                "Bild speichern unter",
                self,
                Gtk.FileChooserAction.SAVE,
                "Speichern",
                "Abbrechen"
            )
            fc.set_current_name(self.image_name)

            def on_fc_response(d, response):
                if response == Gtk.ResponseType.ACCEPT:
                    dest_file = d.get_file()
                    if dest_file:
                        dest_path = dest_file.get_path()
                        if dest_path:
                            shutil.copyfile(self.image_path, dest_path)
                d.destroy()

            fc.connect("response", on_fc_response)
            fc.show()
