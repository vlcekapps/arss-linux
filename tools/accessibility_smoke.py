#!/usr/bin/env python3
"""Launch ARSS and assert its essential AT-SPI contract.

Run this inside the graphical user session.  It deliberately uses temporary XDG
directories, does no network work and terminates the child application after
the accessibility tree is inspected.
"""

from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time

import gi

gi.require_version("Atspi", "2.0")
from gi.repository import Atspi, GLib  # noqa: E402


PROJECT = Path(__file__).resolve().parent.parent


def children(node: Atspi.Accessible) -> list[Atspi.Accessible]:
    result: list[Atspi.Accessible] = []
    try:
        count = node.get_child_count()
    except GLib.Error:
        return result
    for index in range(count):
        try:
            child = node.get_child_at_index(index)
        except GLib.Error:
            continue
        if child is not None:
            result.append(child)
    return result


def walk(root: Atspi.Accessible) -> list[Atspi.Accessible]:
    found: list[Atspi.Accessible] = []
    seen: set[Atspi.Accessible] = set()
    pending = [root]
    while pending and len(found) < 2_000:
        current = pending.pop()
        if current in seen:
            continue
        seen.add(current)
        found.append(current)
        pending.extend(reversed(children(current)))
    return found


def safe_name(node: Atspi.Accessible) -> str:
    try:
        return node.get_name() or ""
    except GLib.Error:
        return ""


def safe_role(node: Atspi.Accessible) -> Atspi.Role:
    try:
        return node.get_role()
    except GLib.Error:
        return Atspi.Role.INVALID


def safe_description(node: Atspi.Accessible) -> str:
    try:
        return node.get_description() or ""
    except GLib.Error:
        return ""


def safe_action_count(node: Atspi.Accessible) -> int:
    try:
        return node.get_n_actions()
    except GLib.Error:
        return 0


def is_native_menu_button_wrapper(
    node: Atspi.Accessible,
    name: str,
    role: Atspi.Role,
    has_popup: bool,
    actions: int,
) -> bool:
    """Accept only the named GTK wrapper around an actionable MenuButton child."""

    if (
        role is not Atspi.Role.PUSH_BUTTON
        or not has_popup
        or actions > 0
        or not (
            name
            in {
                "RSS options",
                "Podcast options",
                "Možnosti RSS",
                "Možnosti podcastů",
            }
            or name.startswith("Options: ")
            or name.startswith("Možnosti: ")
        )
    ):
        return False
    return any(
        safe_name(descendant).strip() == name
        and safe_role(descendant)
        in {Atspi.Role.PUSH_BUTTON_MENU, Atspi.Role.TOGGLE_BUTTON}
        and safe_action_count(descendant) > 0
        for descendant in walk(node)[1:]
    )


def find_application() -> Atspi.Accessible | None:
    desktop = Atspi.get_desktop(0)
    for application in children(desktop):
        nodes = walk(application)
        if any(safe_role(node) is Atspi.Role.FRAME and safe_name(node) == "ARSS" for node in nodes):
            return application
    return None


def inspect(application: Atspi.Accessible) -> None:
    nodes = walk(application)
    by_role: dict[Atspi.Role, list[Atspi.Accessible]] = {}
    for node in nodes:
        by_role.setdefault(safe_role(node), []).append(node)

    tabs = by_role.get(Atspi.Role.PAGE_TAB, [])
    tab_names = {safe_name(tab) for tab in tabs}
    if len(tabs) != 4:
        raise AssertionError(f"Expected four tabs, got {len(tabs)}: {tab_names}")
    expected_english = {"RSS", "Podcasts", "TV guide", "Settings"}
    expected_czech = {"RSS", "Podcasty", "TV program", "Nastavení"}
    if frozenset(tab_names) not in {
        frozenset(expected_english),
        frozenset(expected_czech),
    }:
        raise AssertionError(f"Main tabs are missing or unnamed: {tab_names}")
    selected_tabs: list[str] = []
    focused_tabs: list[str] = []
    not_focusable: list[str] = []
    for tab in tabs:
        name = safe_name(tab)
        try:
            states = tab.get_state_set()
            if states.contains(Atspi.StateType.SELECTED):
                selected_tabs.append(name)
            if states.contains(Atspi.StateType.FOCUSED):
                focused_tabs.append(name)
            if not states.contains(Atspi.StateType.FOCUSABLE):
                not_focusable.append(name)
        except GLib.Error:
            not_focusable.append(name)
    if not_focusable:
        raise AssertionError(
            f"Every main tab must be keyboard focusable: {not_focusable}"
        )
    if selected_tabs != ["RSS"]:
        raise AssertionError(
            f"The initial RSS tab must be the only selected tab: {selected_tabs}"
        )
    if focused_tabs != ["RSS"]:
        raise AssertionError(
            "The named RSS tab must receive initial keyboard/Orca focus; "
            f"focused tabs: {focused_tabs}"
        )
    headings = by_role.get(Atspi.Role.HEADING, [])
    heading_names = {safe_name(item) for item in headings}
    if len(headings) != 1 or not heading_names.intersection({"RSS feeds", "RSS kanály"}):
        raise AssertionError(
            "Only the selected page heading must be exposed; "
            f"got {len(headings)} headings: {heading_names}"
        )
    entries = by_role.get(Atspi.Role.ENTRY, [])
    if not any("RSS" in safe_name(entry) for entry in entries):
        raise AssertionError("The RSS filter entry has no accessible name")
    buttons = by_role.get(Atspi.Role.PUSH_BUTTON, []) + by_role.get(Atspi.Role.PUSH_BUTTON_MENU, [])
    if len(buttons) < 5:
        raise AssertionError(f"Too few keyboard actions are exposed: {len(buttons)}")


def synthesize_key(keyval: int, text: str = "") -> bool:
    if Atspi.generate_keyboard_event(
        keyval,
        None,
        Atspi.KeySynthType.SYM,
    ):
        return True
    try:
        device = Atspi.DeviceA11yManager.try_new()
        if device is None:
            return False
        capabilities = device.get_capabilities()
        device.set_capabilities(
            capabilities | Atspi.DeviceCapability.KEYBOARD_SYNTH
        )
        device.notify_key(True, 0, keyval, 0, text)
        device.notify_key(False, 0, keyval, 0, text)
        # notify_key() reports whether an AT listener consumed the event, not
        # whether the compositor delivered it; callers verify the resulting
        # focus or popup state.
        return True
    except (GLib.Error, AttributeError):
        return False


def synthesize_enter() -> bool:
    return synthesize_key(0xFF0D, "\r")


def activate(node: Atspi.Accessible) -> None:
    try:
        actions = node.get_n_actions()
    except GLib.Error:
        actions = 0
    if actions < 1:
        try:
            has_popup = node.get_state_set().contains(Atspi.StateType.HAS_POPUP)
        except GLib.Error:
            has_popup = False
        if has_popup:
            ancestor: Atspi.Accessible | None = node
            while ancestor is not None and safe_role(ancestor) is not Atspi.Role.FRAME:
                try:
                    ancestor = ancestor.get_parent()
                except GLib.Error:
                    ancestor = None
            if ancestor is not None:
                try:
                    ancestor.grab_focus()
                except GLib.Error:
                    pass
                time.sleep(0.1)
            try:
                node.grab_focus()
            except GLib.Error:
                pass
            deadline = time.monotonic() + 1.0
            while time.monotonic() < deadline:
                try:
                    if node.get_state_set().contains(Atspi.StateType.FOCUSED):
                        break
                except GLib.Error:
                    break
                time.sleep(0.05)
            if synthesize_enter():
                return
        try:
            parent = node.get_parent()
            index = node.get_index_in_parent()
            if parent is not None and parent.is_selection() and parent.select_child(index):
                return
        except GLib.Error:
            pass
        ancestry: list[tuple[str, str, int, list[str], dict[str, str]]] = []
        current: Atspi.Accessible | None = node
        for _index in range(4):
            if current is None:
                break
            try:
                count = current.get_n_actions()
            except GLib.Error:
                count = 0
            try:
                states = [str(state) for state in current.get_state_set().get_states()]
                attributes = dict(current.get_attributes())
            except GLib.Error:
                states = []
                attributes = {}
            ancestry.append(
                (str(safe_role(current)), safe_name(current), count, states, attributes)
            )
            try:
                current = current.get_parent()
            except GLib.Error:
                break
        raise AssertionError(
            f"{safe_name(node)!r} has no accessible action: {ancestry}"
        )
    if not node.do_action(0):
        raise AssertionError(f"Accessible action failed for {safe_name(node)!r}")


def click(node: Atspi.Accessible) -> None:
    try:
        bounds = node.get_extents(Atspi.CoordType.SCREEN)
    except GLib.Error as error:
        raise AssertionError(f"Cannot locate {safe_name(node)!r}") from error
    if bounds.width <= 0 or bounds.height <= 0:
        raise AssertionError(f"{safe_name(node)!r} has no clickable bounds")
    if not Atspi.generate_mouse_event(
        bounds.x + bounds.width // 2,
        bounds.y + bounds.height // 2,
        "b1c",
    ):
        raise AssertionError(f"Pointer activation failed for {safe_name(node)!r}")


def open_popup(
    application: Atspi.Accessible,
    button: Atspi.Accessible,
    predicate,
) -> list[Atspi.Accessible]:
    """Try the real keyboard path, with an X11 pointer fallback for CI."""

    activate(button)
    try:
        return wait_until(application, predicate, timeout=1.0)
    except AssertionError:
        # Some nested test sessions deny synthesized keyboard input even over
        # Xwayland. The deterministic unit test covers Enter/Space directly;
        # this fallback still verifies the resulting AT-SPI menu structure.
        click(button)
        return wait_until(application, predicate)


def wait_until(
    application: Atspi.Accessible,
    predicate,
    *,
    timeout: float = 5.0,
) -> list[Atspi.Accessible]:
    deadline = time.monotonic() + timeout
    latest: list[Atspi.Accessible] = []
    while time.monotonic() < deadline:
        latest = walk(application)
        if predicate(latest):
            return latest
        time.sleep(0.05)
    raise AssertionError("The expected accessibility state did not appear")


def is_native_list_item(node: Atspi.Accessible) -> bool:
    if safe_role(node) is not Atspi.Role.LIST_ITEM:
        return False
    try:
        parent = node.get_parent()
    except GLib.Error:
        return False
    return (
        parent is not None
        and safe_role(parent) is Atspi.Role.LIST
        and bool(safe_name(parent).strip())
    )


def inspect_focus_contract(application: Atspi.Accessible) -> None:
    """Reject silent wrapper stops and controls without an operable interface."""

    forbidden_roles = {Atspi.Role.PANEL}
    action_roles = {
        Atspi.Role.PAGE_TAB,
        Atspi.Role.PUSH_BUTTON,
        Atspi.Role.PUSH_BUTTON_MENU,
        Atspi.Role.TOGGLE_BUTTON,
        Atspi.Role.CHECK_BOX,
        Atspi.Role.RADIO_BUTTON,
        Atspi.Role.MENU_ITEM,
        Atspi.Role.LINK,
    }
    value_roles = {Atspi.Role.SLIDER, Atspi.Role.SPIN_BUTTON}
    composite_roles = {
        Atspi.Role.COMBO_BOX,
        Atspi.Role.LIST,
        Atspi.Role.MENU,
        Atspi.Role.SWITCH,
    }
    text_roles = {
        Atspi.Role.ENTRY,
        Atspi.Role.LABEL,
        Atspi.Role.PASSWORD_TEXT,
        Atspi.Role.TEXT,
    }
    failures: list[tuple[str, str, str]] = []
    for node in walk(application):
        try:
            states = node.get_state_set()
            if not states.contains(Atspi.StateType.FOCUSABLE):
                continue
            has_popup = states.contains(Atspi.StateType.HAS_POPUP)
        except GLib.Error:
            continue
        role = safe_role(node)
        name = safe_name(node).strip()
        if not name:
            failures.append((str(role), name, "missing name"))
            continue
        if role in forbidden_roles:
            failures.append((str(role), name, "focusable wrapper"))
            continue
        if role is Atspi.Role.LIST_ITEM:
            duplicate = False
            for descendant in walk(node)[1:]:
                try:
                    descendant_focusable = descendant.get_state_set().contains(
                        Atspi.StateType.FOCUSABLE
                    )
                except GLib.Error:
                    descendant_focusable = False
                if descendant_focusable and safe_name(descendant).strip() == name:
                    duplicate = True
                    break
            if duplicate:
                failures.append((str(role), name, "duplicate primary focus"))
                continue
        actions = safe_action_count(node)
        try:
            has_value = node.is_value()
        except GLib.Error:
            has_value = False
        try:
            has_text = node.is_text()
        except GLib.Error:
            has_text = False
        try:
            has_selection = node.is_selection()
        except GLib.Error:
            has_selection = False
        operable = (
            role in action_roles
            and actions > 0
            or role in value_roles
            and has_value
            or role in text_roles
            and has_text
            or role in composite_roles
            and (actions > 0 or has_value or has_selection)
            or is_native_list_item(node)
            or is_native_menu_button_wrapper(
                node,
                name,
                role,
                has_popup,
                actions,
            )
        )
        if not operable:
            failures.append((str(role), name, "no action/value/text interface"))
    if failures:
        raise AssertionError(f"Invalid keyboard focus stops: {failures}")


def inspect_settings(application: Atspi.Accessible) -> None:
    nodes = walk(application)
    settings = next(
        (
            node
            for node in nodes
            if safe_role(node) is Atspi.Role.PAGE_TAB
            and safe_name(node) in {"Settings", "Nastavení"}
        ),
        None,
    )
    if settings is None:
        raise AssertionError("The Settings tab is missing")
    activate(settings)
    expected_headings = (
        {"Settings", "Common settings", "RSS settings", "Podcast settings", "About ARSS"},
        {"Nastavení", "Společná nastavení", "Nastavení RSS", "Nastavení podcastů", "O aplikaci ARSS"},
    )

    def settings_ready(current: list[Atspi.Accessible]) -> bool:
        names = [
            safe_name(node)
            for node in current
            if safe_role(node) is Atspi.Role.HEADING
        ]
        return len(names) == 5 and set(names) in expected_headings

    nodes = wait_until(application, settings_ready)
    headings = [node for node in nodes if safe_role(node) is Atspi.Role.HEADING]
    if len(headings) != 5:
        raise AssertionError(
            "Only five Settings headings should be exposed, got "
            f"{len(headings)}: {[safe_name(item) for item in headings]}"
        )
    for item in headings:
        try:
            attributes = item.get_attributes()
        except GLib.Error as error:
            raise AssertionError("A heading has no accessible attributes") from error
        if str(attributes.get("level", "")) not in {"1", "2"}:
            raise AssertionError(
                f"Heading {safe_name(item)!r} has no semantic level: {attributes}"
            )
    dropdowns = [node for node in nodes if safe_role(node) is Atspi.Role.COMBO_BOX]
    if len(dropdowns) != 3:
        raise AssertionError(f"Expected three Settings drop-downs, got {len(dropdowns)}")
    unnamed = [node for node in dropdowns if not safe_name(node).strip()]
    if unnamed:
        raise AssertionError(f"Settings contains {len(unnamed)} unnamed drop-downs")
    background_switches = [
        node
        for node in nodes
        if safe_role(node) is Atspi.Role.SWITCH
        and safe_name(node) in {"Background checks", "Kontroly na pozadí"}
    ]
    if len(background_switches) != 1:
        raise AssertionError(
            "Settings must expose one named background-check switch, got "
            f"{[(safe_name(node), str(safe_role(node))) for node in nodes if safe_role(node) is Atspi.Role.SWITCH]}"
        )
    background_switch = background_switches[0]
    try:
        focusable = background_switch.get_state_set().contains(
            Atspi.StateType.FOCUSABLE
        )
    except GLib.Error:
        focusable = False
    if not focusable or not safe_description(background_switch).strip():
        raise AssertionError(
            "The background-check switch must be focusable and have a description"
        )
    try:
        key_bindings = [
            background_switch.get_key_binding(index).strip()
            for index in range(background_switch.get_n_actions())
        ]
    except GLib.Error:
        key_bindings = []
    nonempty_bindings = [
        binding
        for binding in key_bindings
        if any(part.strip() for part in binding.split(";"))
    ]
    if nonempty_bindings:
        raise AssertionError(
            "The background-check switch must not expose an ambiguous "
            f"single-letter mnemonic: {nonempty_bindings}"
        )
    previews = [
        node
        for node in nodes
        if safe_role(node) is Atspi.Role.PUSH_BUTTON
        and safe_name(node)
        in {
            "Play RSS sound preview",
            "Play podcast sound preview",
            "Přehrát ukázku zvuku RSS",
            "Přehrát ukázku zvuku podcastů",
        }
    ]
    if previews:
        raise AssertionError(
            "Settings must not expose application-specific notification sound "
            f"preview buttons: {[safe_name(node) for node in previews]}"
        )
    expected_english = {
        "Application language": "l",
        "Automatic RSS check": "r",
        "Automatic podcast check": "p",
    }
    expected_czech = {
        "Jazyk aplikace": "j",
        "Automatická kontrola RSS": "r",
        "Automatická kontrola podcastů": "p",
    }
    names = {safe_name(control) for control in dropdowns}
    if set(expected_english).issubset(names):
        expected = expected_english
    elif set(expected_czech).issubset(names):
        expected = expected_czech
    else:
        raise AssertionError(f"Settings controls have ambiguous names: {sorted(names)}")


def inspect_subscription_menu(application: Atspi.Accessible) -> None:
    expected_page_items = (
        {
            "Add address",
            "Search directory",
            "Import OPML",
            "Export OPML",
        },
        {
            "Přidat adresu",
            "Vyhledat v katalogu",
            "Importovat OPML",
            "Exportovat OPML",
        },
    )

    def menu_names(current: list[Atspi.Accessible]) -> list[str]:
        return [
            safe_name(node).strip()
            for node in current
            if safe_role(node) is Atspi.Role.MENU
        ]

    def includes_expected_page_items(
        current: list[Atspi.Accessible],
    ) -> bool:
        names = {
            safe_name(node)
            for node in current
            if safe_role(node) is Atspi.Role.MENU_ITEM
        }
        return any(
            expected.issubset(names)
            for expected in expected_page_items
        )

    nodes = walk(application)
    more_candidates = [
        node
        for node in nodes
        if safe_name(node) in {"RSS options", "Možnosti RSS"}
        and safe_role(node)
        in {
            Atspi.Role.PUSH_BUTTON,
            Atspi.Role.PUSH_BUTTON_MENU,
            Atspi.Role.TOGGLE_BUTTON,
        }
    ]
    more_button = max(
        more_candidates,
        key=safe_action_count,
        default=None,
    )
    if more_button is None:
        raise AssertionError("The named RSS options menu button is missing")
    nodes = open_popup(
        application,
        more_button,
        includes_expected_page_items,
    )
    rss_menu_names = menu_names(nodes)
    if safe_name(more_button) not in rss_menu_names:
        raise AssertionError(
            "The RSS options MENU itself must share its button's accessible "
            f"name; got {rss_menu_names}"
        )
    page_items = {
        safe_name(node)
        for node in nodes
        if safe_role(node) is Atspi.Role.MENU_ITEM
    }
    if not any(
        expected.issubset(page_items)
        for expected in expected_page_items
    ):
        raise AssertionError(f"The RSS options menu is incomplete: {page_items}")
    # Activating the actionable child of the native menu button again closes
    # its popover and restores access to the subscription-row Options button.
    activate(more_button)
    wait_until(
        application,
        lambda current: not any(
            safe_role(node) is Atspi.Role.MENU_ITEM
            and safe_name(node) in page_items
            for node in current
        ),
    )
    nodes = walk(application)
    candidates = [
        node
        for node in nodes
        if safe_role(node)
        in {Atspi.Role.PUSH_BUTTON, Atspi.Role.PUSH_BUTTON_MENU, Atspi.Role.TOGGLE_BUTTON}
        and (
            safe_name(node).startswith("Options: ")
            or safe_name(node).startswith("Možnosti: ")
        )
    ]
    menu_button = next(
        (node for node in candidates if safe_role(node) is Atspi.Role.TOGGLE_BUTTON),
        candidates[0] if candidates else None,
    )
    if menu_button is None:
        exposed_buttons = [
            (str(safe_role(node)), safe_name(node))
            for node in nodes
            if safe_role(node)
            in {Atspi.Role.PUSH_BUTTON, Atspi.Role.PUSH_BUTTON_MENU, Atspi.Role.MENU}
        ]
        raise AssertionError(
            "No named subscription Options menu button is exposed: "
            f"{exposed_buttons}"
        )
    try:
        nodes = open_popup(
            application,
            menu_button,
            lambda current: any(safe_role(node) is Atspi.Role.MENU for node in current)
            and len(
                [node for node in current if safe_role(node) is Atspi.Role.MENU_ITEM]
            )
            >= 5,
        )
    except AssertionError as error:
        exposed = [
            (str(safe_role(node)), safe_name(node))
            for node in walk(application)
            if safe_name(node).strip()
        ]
        raise AssertionError(f"Native subscription menu was not exposed: {exposed}") from error
    subscription_menu_names = menu_names(nodes)
    if safe_name(menu_button) not in subscription_menu_names:
        raise AssertionError(
            "The subscription MENU itself must share its button's accessible "
            f"name; got {subscription_menu_names}"
        )
    menu_items = [
        safe_name(node)
        for node in nodes
        if safe_role(node) is Atspi.Role.MENU_ITEM
    ]
    if any(not name.strip() for name in menu_items):
        def text_value(node: Atspi.Accessible) -> str:
            try:
                return node.get_text(0, -1) if node.is_text() else ""
            except GLib.Error:
                return ""

        def action_values(node: Atspi.Accessible) -> list[str]:
            try:
                return [node.get_action_name(index) for index in range(node.get_n_actions())]
            except GLib.Error:
                return []

        details = [
            (
                safe_name(node),
                text_value(node),
                action_values(node),
                [
                    (
                        str(safe_role(child)),
                        safe_name(child),
                        text_value(child),
                        action_values(child),
                    )
                    for child in walk(node)[1:]
                ],
            )
            for node in nodes
            if safe_role(node) is Atspi.Role.MENU_ITEM
        ]
        raise AssertionError(f"The subscription menu has unnamed items: {details}")
    # Opening a different page closes the native row popover; no synthetic
    # Escape event is needed and the test remains keyboard-layout independent.
    nodes = walk(application)
    podcast = next(
        node
        for node in nodes
        if safe_role(node) is Atspi.Role.PAGE_TAB
        and safe_name(node) in {"Podcasts", "Podcasty"}
    )
    activate(podcast)
    nodes = wait_until(
        application,
        lambda current: any(
            safe_role(node) is Atspi.Role.PAGE_TAB
            and safe_name(node) in {"Podcasts", "Podcasty"}
            and node.get_state_set().contains(Atspi.StateType.SELECTED)
            and node.get_state_set().contains(Atspi.StateType.FOCUSED)
            for node in current
        ),
    )
    nodes = wait_until(
        application,
        lambda current: any(
            safe_role(node) is Atspi.Role.HEADING
            and safe_name(node) in {"Podcasts", "Podcasty"}
            for node in current
        ),
    )
    podcast_candidates = [
        node
        for node in nodes
        if safe_name(node) in {"Podcast options", "Možnosti podcastů"}
        and safe_role(node)
        in {
            Atspi.Role.PUSH_BUTTON,
            Atspi.Role.PUSH_BUTTON_MENU,
            Atspi.Role.TOGGLE_BUTTON,
        }
    ]
    podcast_options = max(
        podcast_candidates,
        key=safe_action_count,
        default=None,
    )
    if podcast_options is None:
        raise AssertionError("The named Podcast options menu button is missing")
    nodes = open_popup(
        application,
        podcast_options,
        includes_expected_page_items,
    )
    podcast_menu_names = menu_names(nodes)
    if safe_name(podcast_options) not in podcast_menu_names:
        raise AssertionError(
            "The Podcast options MENU itself must share its button's accessible "
            f"name; got {podcast_menu_names}"
        )
    podcast_items = {
        safe_name(node)
        for node in nodes
        if safe_role(node) is Atspi.Role.MENU_ITEM
    }
    if not includes_expected_page_items(nodes):
        raise AssertionError(
            f"The Podcast options menu is incomplete: {podcast_items}"
        )


def inspect_invalid_form(application: Atspi.Accessible) -> None:
    nodes = walk(application)
    rss = next(
        node
        for node in nodes
        if safe_role(node) is Atspi.Role.PAGE_TAB and safe_name(node) == "RSS"
    )
    activate(rss)
    nodes = wait_until(
        application,
        lambda current: any(
            safe_role(node) is Atspi.Role.HEADING
            and safe_name(node) in {"RSS feeds", "RSS kanály"}
            for node in current
        ),
    )
    options_candidates = [
        node
        for node in nodes
        if safe_name(node) in {"RSS options", "Možnosti RSS"}
        and safe_role(node)
        in {
            Atspi.Role.PUSH_BUTTON,
            Atspi.Role.PUSH_BUTTON_MENU,
            Atspi.Role.TOGGLE_BUTTON,
        }
    ]
    options = max(
        options_candidates,
        key=safe_action_count,
        default=None,
    )
    if options is None:
        raise AssertionError("The RSS options menu button is missing")
    nodes = open_popup(
        application,
        options,
        lambda current: any(
            safe_role(node) is Atspi.Role.MENU_ITEM
            and safe_name(node) in {"Add address", "Přidat adresu"}
            for node in current
        ),
    )
    add = next(
        node
        for node in nodes
        if safe_role(node) is Atspi.Role.MENU_ITEM
        and safe_name(node) in {"Add address", "Přidat adresu"}
    )
    activate(add)
    nodes = wait_until(
        application,
        lambda current: any(
            safe_role(node) is Atspi.Role.HEADING
            and safe_name(node)
            in {"New RSS or Atom feed", "Nový RSS nebo Atom kanál"}
            for node in current
        ),
    )
    form_entries = [node for node in nodes if safe_role(node) is Atspi.Role.ENTRY]
    if not form_entries:
        raise AssertionError("The Add address form exposes no entry")
    if not any(
        "feed" in safe_name(node).casefold()
        or "address" in safe_name(node).casefold()
        or "adresa" in safe_name(node).casefold()
        for node in form_entries
    ):
        raise AssertionError(
            "The Add address entry has no useful accessible name: "
            f"{[safe_name(node) for node in form_entries]}"
        )
    submit = next(
        (
            node
            for node in nodes
            if safe_role(node) is Atspi.Role.PUSH_BUTTON
            and safe_name(node) in {"Add", "Přidat"}
        ),
        None,
    )
    if submit is None:
        raise AssertionError("The form submit button is missing")
    activate(submit)

    def invalid_is_exposed(current: list[Atspi.Accessible]) -> bool:
        for node in current:
            if safe_role(node) not in {Atspi.Role.ENTRY, Atspi.Role.TEXT}:
                continue
            try:
                if node.get_state_set().contains(Atspi.StateType.INVALID_ENTRY):
                    return True
            except GLib.Error:
                pass
            try:
                attributes = node.get_attributes()
                if any(
                    "invalid" in str(key).casefold()
                    and str(value).casefold() not in {"", "false", "none"}
                    for key, value in attributes.items()
                ):
                    return True
            except GLib.Error:
                pass
        return False

    try:
        nodes = wait_until(application, invalid_is_exposed)
    except AssertionError as error:
        details = []
        for node in walk(application):
            if safe_role(node) not in {Atspi.Role.ENTRY, Atspi.Role.TEXT}:
                continue
            try:
                states = [str(state) for state in node.get_state_set().get_states()]
                attributes = dict(node.get_attributes())
            except GLib.Error:
                continue
            details.append((safe_name(node), states, attributes))
        raise AssertionError(f"Invalid input state was not exposed: {details}") from error
    nodes = wait_until(
        application,
        lambda current: any(
            safe_role(node) is Atspi.Role.STATUS_BAR and safe_name(node).strip()
            for node in current
        ),
    )
    statuses = [
        safe_name(node)
        for node in nodes
        if safe_role(node) is Atspi.Role.STATUS_BAR and safe_name(node).strip()
    ]
    if not statuses:
        raise AssertionError("Invalid input has no AT-visible textual explanation")

def main() -> int:
    with tempfile.TemporaryDirectory(prefix="arss-atspi-") as temporary:
        environment = os.environ.copy()
        environment["XDG_DATA_HOME"] = str(Path(temporary) / "data")
        environment["XDG_CONFIG_HOME"] = str(Path(temporary) / "config")
        # AT-SPI's legacy key synthesizer cannot inject into a native Wayland
        # client.  Xwayland lets this smoke exercise the real Enter path while
        # the accessibility tree and production app remain toolkit-identical.
        if environment.get("DISPLAY"):
            environment["GDK_BACKEND"] = "x11"
        process = subprocess.Popen(
            [sys.executable, "-m", "arss"],
            cwd=PROJECT,
            env=environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            application = None
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline and process.poll() is None:
                application = find_application()
                if application is not None:
                    break
                time.sleep(0.1)
            if process.poll() is not None:
                output, error = process.communicate()
                raise RuntimeError(f"ARSS exited early ({process.returncode})\n{output}\n{error}")
            if application is None:
                raise RuntimeError("ARSS did not appear in the AT-SPI tree")
            wait_until(
                application,
                lambda current: any(
                    safe_role(node) is Atspi.Role.PAGE_TAB
                    and safe_name(node) == "RSS"
                    and node.get_state_set().contains(
                        Atspi.StateType.FOCUSED
                    )
                    for node in current
                ),
                timeout=2.0,
            )
            inspect(application)
            inspect_focus_contract(application)
            inspect_subscription_menu(application)
            inspect_settings(application)
            inspect_invalid_form(application)
            inspect_focus_contract(application)
            print("AT-SPI smoke test passed")
            return 0
        finally:
            process.terminate()
            try:
                _output, error = process.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                _output, error = process.communicate(timeout=5)
            if error.strip():
                print(error, file=sys.stderr)


if __name__ == "__main__":
    raise SystemExit(main())
