"""GTK helpers shared by the ARSS windows.

The helpers intentionally use standard GTK controls.  That gives Orca native
roles, keyboard interaction and focus handling without a parallel accessibility
tree maintained by the application.
"""

from __future__ import annotations

from collections.abc import Callable

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, Gio, GLib, Gtk, Pango  # noqa: E402


CONTENT_WIDTH = 840
FORM_WIDTH = 720


def _enable_label_reflow(label: Gtk.Label) -> None:
    """Let platform-sized text wrap without changing the control semantics."""

    label.set_wrap(True)
    label.set_wrap_mode(Pango.WrapMode.WORD_CHAR)
    label.set_natural_wrap_mode(Gtk.NaturalWrapMode.WORD)


def _enable_control_label_reflow(
    control: Gtk.Button | Gtk.CheckButton,
) -> None:
    def visit(widget: Gtk.Widget) -> None:
        child = widget.get_first_child()
        while child is not None:
            if isinstance(child, Gtk.Label):
                _enable_label_reflow(child)
            visit(child)
            child = child.get_next_sibling()

    visit(control)


def wrapping_button(label: str, **properties: object) -> Gtk.Button:
    """Create a native button whose visible label can reflow at large text."""

    button = Gtk.Button(label=label, **properties)
    _enable_control_label_reflow(button)
    return button


def wrapping_check_button(
    label: str,
    **properties: object,
) -> Gtk.CheckButton:
    """Create a native check button with a reflowable visible label."""

    button = Gtk.CheckButton(label=label, **properties)
    _enable_control_label_reflow(button)
    return button


class HeadingLabel(Gtk.Label):
    """Label subclass whose class-local accessible role is HEADING."""

    def __init__(self, text: str, *, level: int = 1) -> None:
        super().__init__(label=text, xalign=0, wrap=True)
        _enable_label_reflow(self)
        self.set_focusable(False)
        self.set_accessible_role(Gtk.AccessibleRole.HEADING)
        self.update_property([Gtk.AccessibleProperty.LEVEL], [max(1, level)])
        self.add_css_class("title-1" if level == 1 else "title-2")


class PresentationLabel(Gtk.Label):
    """Visible text intentionally omitted from AT because another control owns it."""

    def __init__(self, **properties: object) -> None:
        super().__init__(**properties)
        if self.get_wrap():
            _enable_label_reflow(self)
        self.set_focusable(False)
        self.set_accessible_role(Gtk.AccessibleRole.PRESENTATION)


class SilentSpinner(Gtk.Spinner):
    """Visual progress only; a sibling LiveStatus exposes the same state."""

    def __init__(self) -> None:
        super().__init__()
        self.set_focusable(False)
        self.set_accessible_role(Gtk.AccessibleRole.PRESENTATION)


class MenuItemButton(Gtk.Button):
    """Menu item with a class-local role and a reliably exposed label."""

    def __init__(self, label: str) -> None:
        super().__init__(label=label, hexpand=True, halign=Gtk.Align.FILL)
        _enable_control_label_reflow(self)
        self.set_accessible_role(Gtk.AccessibleRole.MENU_ITEM)
        self.add_css_class("flat")
        child = self.get_child()
        if isinstance(child, Gtk.Label):
            child.set_xalign(0)


class AccessibleMenuPopover(Gtk.Popover):
    """Small keyboard menu avoiding unnamed GtkModelButton nodes in GTK 4.22."""

    def __init__(self, opener: Gtk.MenuButton, label: str) -> None:
        super().__init__()
        self.opener = opener
        self.items: list[MenuItemButton] = []
        self.set_accessible_role(Gtk.AccessibleRole.MENU)
        self.update_property(
            [Gtk.AccessibleProperty.LABEL],
            [label],
        )
        opener_keys = Gtk.EventControllerKey()
        opener_keys.connect("key-pressed", self._opener_key_pressed)
        opener.add_controller(opener_keys)
        body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        body.set_focusable(False)
        body.set_margin_top(6)
        body.set_margin_bottom(6)
        body.set_margin_start(6)
        body.set_margin_end(6)
        self.set_child(body)
        self.body = body
        self.connect("map", self._mapped)

    def append_item(self, label: str, callback: Callable[[], None]) -> None:
        item = MenuItemButton(label)
        item.connect("clicked", self._activated, callback)
        controller = Gtk.EventControllerKey()
        controller.connect("key-pressed", self._key_pressed)
        item.add_controller(controller)
        self.items.append(item)
        self.body.append(item)

    def _mapped(self, *_args: object) -> None:
        def focus_first() -> bool:
            if self.items:
                self.items[0].grab_focus()
            return GLib.SOURCE_REMOVE

        GLib.idle_add(focus_first)

    def _opener_key_pressed(
        self,
        _controller: Gtk.EventControllerKey,
        keyval: int,
        _keycode: int,
        _state: Gdk.ModifierType,
    ) -> bool:
        if keyval in {Gdk.KEY_Return, Gdk.KEY_KP_Enter, Gdk.KEY_space}:
            self.popup()
            return True
        return False

    def _activated(
        self,
        _button: Gtk.Button,
        callback: Callable[[], None],
    ) -> None:
        self.popdown()
        callback()

    def _key_pressed(
        self,
        _controller: Gtk.EventControllerKey,
        keyval: int,
        _keycode: int,
        _state: Gdk.ModifierType,
    ) -> bool:
        if not self.items:
            return False
        root = self.get_root()
        focused = root.get_focus() if root is not None else None
        try:
            index = self.items.index(focused)
        except ValueError:
            index = 0
        if keyval in {Gdk.KEY_Down, Gdk.KEY_KP_Down}:
            self.items[(index + 1) % len(self.items)].grab_focus()
            return True
        if keyval in {Gdk.KEY_Up, Gdk.KEY_KP_Up}:
            self.items[(index - 1) % len(self.items)].grab_focus()
            return True
        if keyval in {Gdk.KEY_Home, Gdk.KEY_KP_Home}:
            self.items[0].grab_focus()
            return True
        if keyval in {Gdk.KEY_End, Gdk.KEY_KP_End}:
            self.items[-1].grab_focus()
            return True
        if keyval == Gdk.KEY_Escape:
            self.popdown()
            self.opener.grab_focus()
            return True
        return False


def heading(text: str, *, level: int = 1) -> HeadingLabel:
    label = HeadingLabel(text, level=level)
    return label


def description(text: str) -> Gtk.Label:
    label = Gtk.Label(label=text, xalign=0, wrap=True, selectable=False)
    _enable_label_reflow(label)
    label.set_focusable(False)
    label.add_css_class("dim-label")
    return label


def readable_description(text: str) -> Gtk.Label:
    """Create one keyboard-reachable block of standalone descriptive text."""

    label = Gtk.Label(label=text, xalign=0, wrap=True, selectable=True)
    _enable_label_reflow(label)
    label.set_focusable(True)
    label.add_css_class("dim-label")
    return label


def labelled(
    label_text: str,
    widget: Gtk.Widget,
    *,
    mnemonic_widget: Gtk.Widget | None = None,
) -> Gtk.Box:
    """Place a mnemonic label above a form control and link both semantically."""

    label = Gtk.Label(label=label_text, xalign=0, use_underline=True)
    _enable_label_reflow(label)
    label.set_focusable(False)
    target = mnemonic_widget or widget
    label.set_mnemonic_widget(target)
    index = 0
    while index < len(label_text) - 1:
        if label_text[index] != "_":
            index += 1
            continue
        if label_text[index + 1] == "_":
            index += 2
            continue
        target.update_property(
            [Gtk.AccessibleProperty.KEY_SHORTCUTS],
            [f"Alt+{label_text[index + 1].upper()}"],
        )
        break
    box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
    box.set_focusable(False)
    box.append(label)
    box.append(widget)
    return box


def text_button(label: str, callback: Callable[[Gtk.Button], None]) -> Gtk.Button:
    button = wrapping_button(label)
    button.connect("clicked", callback)
    return button


def clear_box(box: Gtk.Box) -> None:
    child = box.get_first_child()
    while child is not None:
        following = child.get_next_sibling()
        box.remove(child)
        child = following


def clear_list(box: Gtk.ListBox | Gtk.ListView) -> None:
    if isinstance(box, Gtk.ListView):
        box._arss_callbacks.clear()
        box._arss_metadata.clear()
        box._arss_bindings.clear()
        box._arss_store.remove_all()
        return
    child = box.get_first_child()
    while child is not None:
        following = child.get_next_sibling()
        box.remove(child)
        child = following


def _list_item_setup(
    _factory: Gtk.SignalListItemFactory,
    list_item: Gtk.ListItem,
) -> None:
    list_item.set_focusable(True)
    list_item.set_selectable(True)


def _list_item_bind(
    _factory: Gtk.SignalListItemFactory,
    list_item: Gtk.ListItem,
    box: Gtk.ListView,
) -> None:
    child = list_item.get_item()
    if not isinstance(child, Gtk.Widget):
        return
    label, description = box._arss_metadata[child]
    list_item.set_accessible_label(label)
    list_item.set_accessible_description(description)
    list_item.set_activatable(child in box._arss_callbacks)
    list_item.set_child(child)
    box._arss_bindings[child] = list_item


def _list_item_unbind(
    _factory: Gtk.SignalListItemFactory,
    list_item: Gtk.ListItem,
    box: Gtk.ListView,
) -> None:
    child = list_item.get_child()
    if child is not None:
        box._arss_bindings.pop(child, None)
    list_item.set_child(None)


def _list_item_activated(box: Gtk.ListView, position: int) -> None:
    child = box._arss_store.get_item(position)
    callback = box._arss_callbacks.get(child)
    if callback is not None:
        callback()


def navigable_list(label: str) -> Gtk.ListView:
    """Create a named GTK list with native item, arrow and Tab semantics."""

    store = Gio.ListStore.new(Gtk.Widget)
    selection = Gtk.SingleSelection(model=store)
    factory = Gtk.SignalListItemFactory()
    box = Gtk.ListView(model=selection, factory=factory)
    box._arss_store = store
    box._arss_callbacks: dict[Gtk.Widget, Callable[[], None]] = {}
    box._arss_metadata: dict[Gtk.Widget, tuple[str, str]] = {}
    box._arss_bindings: dict[Gtk.Widget, Gtk.ListItem] = {}
    factory.connect("setup", _list_item_setup)
    factory.connect("bind", _list_item_bind, box)
    factory.connect("unbind", _list_item_unbind, box)
    box.connect("activate", _list_item_activated)
    box.set_single_click_activate(True)
    box.set_show_separators(True)
    box.set_tab_behavior(Gtk.ListTabBehavior.ITEM)
    box.add_css_class("boxed-list")
    box.add_css_class("rich-list")
    box.update_property([Gtk.AccessibleProperty.LABEL], [label])
    return box


def append_list_item(
    box: Gtk.ListView,
    child: Gtk.Widget,
    *,
    label: str,
    description: str,
    callback: Callable[[], None],
) -> None:
    """Append one activatable list item backed by its visible child widget."""

    box._arss_metadata[child] = (label, description)
    box._arss_callbacks[child] = callback
    box._arss_store.append(child)


def update_list_item(
    box: Gtk.ListView,
    child: Gtk.Widget,
    *,
    label: str,
    description: str,
) -> None:
    """Update both model metadata and the currently bound accessible item."""

    box._arss_metadata[child] = (label, description)
    binding = box._arss_bindings.get(child)
    if binding is not None:
        binding.set_accessible_label(label)
        binding.set_accessible_description(description)


def focus_list_item_later(box: Gtk.ListView, position: int) -> None:
    """Select, scroll to and focus a model item on the next main-loop turn."""

    def apply() -> bool:
        if not 0 <= position < box._arss_store.get_n_items():
            return GLib.SOURCE_REMOVE
        model = box.get_model()
        if isinstance(model, Gtk.SingleSelection):
            model.set_selected(position)
        box.scroll_to(position, Gtk.ListScrollFlags.SELECT, None)
        child = box._arss_store.get_item(position)
        target = child.get_parent() if child is not None else None
        if target is None or not target.grab_focus():
            box.scroll_to(
                position,
                Gtk.ListScrollFlags.FOCUS | Gtk.ListScrollFlags.SELECT,
                None,
            )
        return GLib.SOURCE_REMOVE

    GLib.idle_add(apply)


def list_item_child(box: Gtk.ListView, position: int) -> Gtk.Widget | None:
    """Return the content widget for a model position, primarily for tests."""

    return box._arss_store.get_item(position)


def list_item_focus_widget(
    box: Gtk.ListView,
    position: int,
) -> Gtk.Widget | None:
    """Return the toolkit-owned focusable row for a currently bound item."""

    child = list_item_child(box, position)
    return child.get_parent() if child is not None else None


def focus_exact_later(widget: Gtk.Widget) -> None:
    """Focus this exact native control on the next main-loop turn."""

    GLib.idle_add(lambda: (widget.grab_focus(), GLib.SOURCE_REMOVE)[1])


def focus_later(widget: Gtk.Widget) -> None:
    """Move focus once on the next main-loop turn, never as a repeating idle."""

    def first_focusable(root: Gtk.Widget) -> Gtk.Widget | None:
        # Composite rows can report themselves focusable even when their real
        # action is a child button. Prefer descendants to avoid an empty AT
        # target; a leaf button falls back to itself below.
        child = root.get_first_child()
        while child is not None:
            candidate = first_focusable(child)
            if candidate is not None:
                return candidate
            child = child.get_next_sibling()
        if root.get_focusable() and root.get_sensitive() and root.get_visible():
            return root
        return None

    def apply() -> bool:
        target = first_focusable(widget)
        if target is not None:
            target.grab_focus()
        return GLib.SOURCE_REMOVE

    GLib.idle_add(apply)


class LiveStatus(Gtk.Label):
    """Visible status whose meaningful changes are announced politely by AT."""

    def __init__(self, text: str = "") -> None:
        super().__init__(label=text, xalign=0, wrap=True, selectable=False)
        _enable_label_reflow(self)
        self.set_focusable(False)
        self.set_accessible_role(Gtk.AccessibleRole.STATUS)
        self.set_visible(bool(text))

    def set_status(self, text: str, *, announce: bool = True) -> None:
        changed = self.get_text() != text
        self.set_text(text)
        self.set_visible(bool(text))
        if changed and text and announce:
            self.announce(text, Gtk.AccessibleAnnouncementPriority.MEDIUM)


class ReadableStatus(Gtk.Label):
    """A live message which is also reachable and selectable from the keyboard."""

    def __init__(self, text: str = "") -> None:
        super().__init__(label=text, xalign=0, wrap=True, selectable=True)
        _enable_label_reflow(self)
        self.set_focusable(True)
        self.set_visible(bool(text))

    def set_status(self, text: str, *, announce: bool = True) -> None:
        changed = self.get_text() != text
        self.set_text(text)
        self.set_visible(bool(text))
        if changed and text and announce:
            self.announce(
                text,
                Gtk.AccessibleAnnouncementPriority.MEDIUM,
            )

class BusyBlock(Gtk.Box):
    """Progress indicator plus an AT-visible status string."""

    def __init__(self) -> None:
        super().__init__(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        self.set_focusable(False)
        self.spinner = SilentSpinner()
        self.status = LiveStatus()
        self.append(self.spinner)
        self.append(self.status)
        self.set_visible(False)

    def start(self, message: str) -> None:
        self.set_visible(True)
        self.spinner.start()
        self.status.set_status(message)

    def stop(self) -> None:
        self.spinner.stop()
        self.status.set_status("", announce=False)
        self.set_visible(False)


def set_invalid(widget: Gtk.Widget, invalid: bool) -> None:
    """Keep visual and assistive invalid state in sync for a form control."""

    if invalid:
        widget.add_css_class("error")
    else:
        widget.remove_css_class("error")
    widget.update_state(
        [Gtk.AccessibleState.INVALID],
        [
            int(Gtk.AccessibleInvalidState.TRUE)
            if invalid
            else int(Gtk.AccessibleInvalidState.FALSE)
        ],
    )


def alert(parent: Gtk.Window, title: str, detail: str = "") -> None:
    dialog = Gtk.AlertDialog(message=title, detail=detail, modal=True)
    dialog.show(parent)


def confirm(
    parent: Gtk.Window,
    title: str,
    detail: str,
    cancel_label: str,
    confirm_label: str,
    callback: Callable[[], None],
) -> None:
    dialog = Gtk.AlertDialog(message=title, detail=detail, modal=True)
    dialog.set_buttons([cancel_label, confirm_label])
    dialog.set_cancel_button(0)
    dialog.set_default_button(0)

    def finished(source: Gtk.AlertDialog, result: object) -> None:
        try:
            response = source.choose_finish(result)  # type: ignore[arg-type]
        except Exception:
            return
        if response == 1:
            callback()

    dialog.choose(parent, None, finished)


class FormWindow(Adw.ApplicationWindow):
    """Small modal-like child window with an explicit Back button."""

    def __init__(
        self,
        parent: Adw.ApplicationWindow,
        title: str,
        back_label: str,
        *,
        width: int = FORM_WIDTH,
        height: int = 600,
    ) -> None:
        super().__init__(application=parent.get_application(), transient_for=parent, modal=True)
        self.set_title(title)
        self.set_default_size(width, height)
        root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        root.set_focusable(False)
        bar = Gtk.HeaderBar()
        back = wrapping_button(back_label)
        back.connect("clicked", lambda _button: self.close())
        bar.pack_start(back)
        title_label = Gtk.Label(
            label=title,
            ellipsize=Pango.EllipsizeMode.END,
        )
        title_label.set_hexpand(True)
        title_label.set_tooltip_text(title)
        bar.set_title_widget(title_label)
        root.append(bar)
        self.content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        self.content.set_focusable(False)
        self.content.set_margin_top(18)
        self.content.set_margin_bottom(18)
        self.content.set_margin_start(18)
        self.content.set_margin_end(18)
        self.content_scroll = scrolled_content(self.content)
        self.content_scroll.set_vexpand(True)
        root.append(self.content_scroll)
        self.set_content(root)


def scrolled_content(child: Gtk.Widget, *, propagate_natural_height: bool = False) -> Gtk.ScrolledWindow:
    scroll = Gtk.ScrolledWindow()
    scroll.set_focusable(False)
    scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
    scroll.set_propagate_natural_height(propagate_natural_height)
    scroll.set_child(child)
    return scroll
