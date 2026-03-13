import json
import os
import queue
import subprocess
import threading
import time
from collections import deque

import pygame


WINDOW_WIDTH = 1000
WINDOW_HEIGHT = 660
LOG_MAX_LINES = 600
LOG_LINE_HEIGHT = 20
LOG_PADDING = 10
DEFAULT_PORT = "3000"
SIDE_MARGIN = 24
CONTROL_ROW_Y = 86
TAB_ROW_Y = 140
LOG_TOP_Y = 176
LOG_BOTTOM_MARGIN = 24
PENDING_REFRESH_SEC = 0.8
SERVERS_REFRESH_SEC = 0.8
SERVERS_WINDOW_MS = 3000
BLINK_PULSE_MS = 500

COLORS = {
    "bg_top": (16, 22, 28),
    "bg_bottom": (10, 12, 16),
    "panel": (24, 28, 34),
    "panel_border": (58, 64, 72),
    "text": (230, 230, 230),
    "muted": (160, 165, 175),
    "accent_blue": (70, 160, 230),
    "accent_green": (70, 170, 110),
    "accent_orange": (230, 160, 80),
    "accent_red": (200, 80, 80),
    "accent_purple": (120, 130, 240),
    "shadow": (0, 0, 0, 90),
}


class Button:
    def __init__(self, rect, label, style=None):
        self.rect = pygame.Rect(rect)
        self.label = label
        self.style = style or {}

    def draw(self, surface, font, enabled=True, active=False, hovered=False):
        base = self.style.get("bg", COLORS["panel"])
        border = self.style.get("border", COLORS["panel_border"])
        text_color = self.style.get("text", COLORS["text"])
        if not enabled:
            base = (70, 70, 70)
            border = (90, 90, 90)
            text_color = (200, 200, 200)
        if hovered:
            base = tuple(min(255, c + 15) for c in base)
        if active:
            border = self.style.get("active_border", COLORS["accent_blue"])
        pygame.draw.rect(surface, base, self.rect, border_radius=8)
        pygame.draw.rect(surface, border, self.rect, 2, border_radius=8)
        text = font.render(self.label, True, text_color)
        text_rect = text.get_rect(center=self.rect.center)
        surface.blit(text, text_rect)

    def hit(self, pos):
        return self.rect.collidepoint(pos)


def timestamp():
    return time.strftime("%H:%M:%S")


def make_env(port_text):
    env = os.environ.copy()
    env["PORT"] = port_text
    env["FORCE_PORT_3000"] = "0"
    return env


def start_process(port_text, log_queue, project_dir):
    env = make_env(port_text)
    process = subprocess.Popen(
        ["node", "server.js"],
        cwd=project_dir,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
        env=env,
    )

    def reader(stream, prefix):
        try:
            for line in iter(stream.readline, ""):
                clean = line.rstrip("\n")
                if clean:
                    log_queue.put(f"[{timestamp()}] {prefix}{clean}")
        finally:
            try:
                stream.close()
            except Exception:
                pass

    threading.Thread(target=reader, args=(process.stdout, ""), daemon=True).start()
    threading.Thread(target=reader, args=(process.stderr, "ERR: "), daemon=True).start()
    log_queue.put(f"[{timestamp()}] Started server.js on port {port_text} (pid {process.pid})")
    return process


def stop_process(process, log_queue):
    if not process or process.poll() is not None:
        return

    log_queue.put(f"[{timestamp()}] Stopping server.js (pid {process.pid})")
    try:
        process.terminate()
    except Exception:
        pass

    def waiter():
        try:
            process.wait(timeout=2)
            log_queue.put(f"[{timestamp()}] Server stopped.")
        except Exception:
            try:
                if os.name == "nt":
                    subprocess.run(
                        ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                        check=False,
                    )
                else:
                    process.kill()
                log_queue.put(f"[{timestamp()}] Server force-killed.")
            except Exception:
                log_queue.put(f"[{timestamp()}] Failed to stop server.")

    threading.Thread(target=waiter, daemon=True).start()

def read_local_pending(path):
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
        return data.get("pending", []), data.get("updatedAt"), None
    except FileNotFoundError:
        return [], None, None
    except Exception as err:
        return [], None, str(err)

def queue_local_cancel(path, request_id, log_queue):
    try:
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(json.dumps({"id": request_id}) + "\n")
        log_queue.put(f"[{timestamp()}] Cancelled request {request_id}.")
    except Exception as err:
        log_queue.put(f"[{timestamp()}] Cancel failed: {err}")

def read_local_servers(path):
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
        return data.get("servers", []), data.get("updatedAt"), None
    except FileNotFoundError:
        return [], None, None
    except Exception as err:
        return [], None, str(err)

def clear_local_servers(path):
    payload = {
        "updatedAt": time.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "windowMs": SERVERS_WINDOW_MS,
        "servers": [],
    }
    try:
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(payload, handle)
    except Exception:
        pass

def clear_local_pending(path):
    payload = {"updatedAt": time.strftime("%Y-%m-%dT%H:%M:%SZ"), "count": 0, "pending": []}
    try:
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(payload, handle)
    except Exception:
        pass

def latest_last_seen(servers_list):
    latest = None
    for server in servers_list:
        endpoints = server.get("endpoints", [])
        for endpoint in endpoints:
            last_seen = endpoint.get("lastSeen")
            if isinstance(last_seen, (int, float)):
                latest = last_seen if latest is None else max(latest, last_seen)
    return latest

def build_endpoint_tree(endpoints):
    root = {"children": {}, "lastSend": 0, "isEndpoint": False}
    for endpoint in endpoints:
        path = endpoint.get("endpoint") or ""
        last_send = endpoint.get("lastSend") or 0
        parts = [part for part in path.split("/") if part]
        if not parts:
            parts = ["(root)"]
        node = root
        for part in parts:
            if part not in node["children"]:
                node["children"][part] = {"children": {}, "lastSend": 0, "isEndpoint": False}
            node = node["children"][part]
        node["lastSend"] = max(node["lastSend"], last_send)
        node["isEndpoint"] = True
    return root

def flatten_endpoint_tree(node, depth, rows, prefix, ip):
    for name in sorted(node["children"].keys()):
        child = node["children"][name]
        has_children = len(child["children"]) > 0
        full_path = name if not prefix else f"{prefix}/{name}"
        rows.append(
            {
                "type": "node",
                "text": name,
                "depth": depth,
                "path": full_path,
                "ip": ip,
                "lastSend": child.get("lastSend", 0),
                "isEndpoint": child.get("isEndpoint", False),
                "hasChildren": has_children,
            }
        )
        flatten_endpoint_tree(child, depth + 1, rows, full_path, ip)

def build_server_rows(servers_items, servers_expanded):
    rows = []
    for server in servers_items:
        ip = server.get("ip", "-")
        rows.append({"type": "ip", "ip": ip, "text": f"IP: {ip}"})
        if not servers_expanded.get(ip, True):
            continue
        endpoints = server.get("endpoints", [])
        if not endpoints:
            rows.append(
                {
                    "type": "node",
                    "text": "(no endpoints)",
                    "depth": 0,
                    "lastSend": 0,
                    "isLeaf": True,
                }
            )
            continue
        tree = build_endpoint_tree(endpoints)
        flatten_endpoint_tree(tree, 0, rows, "", ip)
    return rows

def draw_vertical_gradient(surface, rect, top_color, bottom_color):
    x, y, w, h = rect
    for i in range(h):
        t = i / max(1, h - 1)
        color = (
            int(top_color[0] + (bottom_color[0] - top_color[0]) * t),
            int(top_color[1] + (bottom_color[1] - top_color[1]) * t),
            int(top_color[2] + (bottom_color[2] - top_color[2]) * t),
        )
        pygame.draw.line(surface, color, (x, y + i), (x + w, y + i))

def draw_panel(surface, rect):
    shadow_rect = pygame.Rect(rect.x + 4, rect.y + 4, rect.width, rect.height)
    shadow_surface = pygame.Surface((shadow_rect.width, shadow_rect.height), pygame.SRCALPHA)
    shadow_surface.fill(COLORS["shadow"])
    surface.blit(shadow_surface, shadow_rect.topleft)
    pygame.draw.rect(surface, COLORS["panel"], rect, border_radius=10)
    pygame.draw.rect(surface, COLORS["panel_border"], rect, 2, border_radius=10)



def main():
    pygame.init()
    pygame.display.set_caption("InfinityNet Control Panel")
    screen = pygame.display.set_mode((WINDOW_WIDTH, WINDOW_HEIGHT))
    clock = pygame.time.Clock()
    ui_font = pygame.font.SysFont("Segoe UI", 16)
    mono_font = pygame.font.SysFont("Cascadia Mono", 15)
    header_font = pygame.font.SysFont("Segoe UI Semibold", 22)

    project_dir = os.path.dirname(os.path.abspath(__file__))

    log_queue = queue.Queue()
    log_lines = deque(maxlen=LOG_MAX_LINES)
    log_scroll_offset = 0
    log_follow = True
    pending_scroll_offset = 0
    pending_items = []
    pending_last_update = None
    pending_error = None
    last_pending_fetch = 0.0
    servers_items = []
    servers_last_update = None
    servers_error = None
    servers_scroll_offset = 0
    last_servers_fetch = 0.0
    servers_total_rows = 0
    servers_cache = []
    servers_empty_streak = 0
    servers_rows = []
    servers_last_send_seen = {}
    servers_blink_until = {}
    servers_expanded = {}
    servers_header_rects = []

    process = None
    running_port = None

    local_pending_path = os.path.join(project_dir, "local_pending.json")
    local_cancel_path = os.path.join(project_dir, "local_cancel.jsonl")
    local_servers_path = os.path.join(project_dir, "local_servers.json")

    port_input = DEFAULT_PORT
    port_active = False

    toggle_button = Button(
        (SIDE_MARGIN, CONTROL_ROW_Y, 140, 40),
        "Start",
        {"bg": COLORS["accent_green"], "border": (35, 120, 80), "text": COLORS["text"]},
    )
    restart_button = Button(
        (SIDE_MARGIN + 152, CONTROL_ROW_Y, 140, 40),
        "Restart",
        {"bg": COLORS["accent_orange"], "border": (140, 90, 35), "text": (20, 20, 20)},
    )
    clear_button = Button(
        (SIDE_MARGIN + 304, CONTROL_ROW_Y, 150, 40),
        "Clear Logs",
        {"bg": (70, 80, 95), "border": (90, 100, 120), "text": COLORS["text"]},
    )
    logs_tab = Button(
        (SIDE_MARGIN, TAB_ROW_Y, 100, 28),
        "Logs",
        {"bg": (40, 50, 62), "border": COLORS["panel_border"], "text": COLORS["text"]},
    )
    pending_tab = Button(
        (SIDE_MARGIN + 110, TAB_ROW_Y, 120, 28),
        "Pending",
        {"bg": (40, 50, 62), "border": COLORS["panel_border"], "text": COLORS["text"]},
    )
    servers_tab = Button(
        (SIDE_MARGIN + 240, TAB_ROW_Y, 120, 28),
        "Servers",
        {"bg": (40, 50, 62), "border": COLORS["panel_border"], "text": COLORS["text"]},
    )
    bottom_button = Button(
        (SIDE_MARGIN + 370, TAB_ROW_Y, 120, 28),
        "Bottom",
        {"bg": COLORS["accent_blue"], "border": (40, 120, 180), "text": COLORS["text"]},
    )

    log_area = pygame.Rect(
        SIDE_MARGIN,
        LOG_TOP_Y,
        WINDOW_WIDTH - SIDE_MARGIN * 2,
        WINDOW_HEIGHT - LOG_TOP_Y - LOG_BOTTOM_MARGIN,
    )
    port_box = pygame.Rect(WINDOW_WIDTH - SIDE_MARGIN - 140, CONTROL_ROW_Y, 140, 40)
    active_tab = "logs"
    pending_cancel_rects = []

    def append_log(line):
        log_lines.append(line)

    def flush_logs():
        nonlocal log_scroll_offset, log_follow
        new_lines = 0
        while True:
            try:
                line = log_queue.get_nowait()
            except queue.Empty:
                break
            append_log(line)
            new_lines += 1
        if log_follow:
            log_scroll_offset = 0
        else:
            log_scroll_offset += new_lines

    def validate_port(text):
        if not text.isdigit():
            return False
        value = int(text)
        return 1 <= value <= 65535

    running = True
    while running:
        visible_lines = max(1, (log_area.height - 32) // LOG_LINE_HEIGHT)
        pending_cancel_rects = []
        flush_logs()
        max_log_offset = max(0, len(log_lines) - visible_lines)
        log_scroll_offset = max(0, min(log_scroll_offset, max_log_offset))
        if log_scroll_offset == 0:
            log_follow = True

        if active_tab == "pending":
            now = time.time()
            if now - last_pending_fetch > PENDING_REFRESH_SEC:
                last_pending_fetch = now
                pending_items, pending_last_update, pending_error = read_local_pending(
                    local_pending_path
                )
            max_pending_offset = max(0, len(pending_items) - visible_lines)
            pending_scroll_offset = max(0, min(pending_scroll_offset, max_pending_offset))

        if active_tab == "servers":
            now = time.time()
            if now - last_servers_fetch > SERVERS_REFRESH_SEC:
                last_servers_fetch = now
                servers_items, servers_last_update, servers_error = read_local_servers(
                    local_servers_path
                )
                if servers_items:
                    servers_empty_streak = 0
                    servers_cache = servers_items
                else:
                    servers_empty_streak += 1
                    cached_latest = latest_last_seen(servers_cache)
                    if cached_latest and (time.time() * 1000 - cached_latest) <= SERVERS_WINDOW_MS:
                        if servers_empty_streak < 2:
                            servers_items = servers_cache
                        else:
                            servers_cache = []
                    else:
                        servers_cache = []
                for server in servers_items:
                    ip = server.get("ip")
                    if ip and ip not in servers_expanded:
                        servers_expanded[ip] = True
            servers_rows = build_server_rows(servers_items, servers_expanded)
            servers_total_rows = len(servers_rows)
            max_servers_offset = max(0, servers_total_rows - visible_lines)
            servers_scroll_offset = max(0, min(servers_scroll_offset, max_servers_offset))

        if process and process.poll() is not None:
            log_queue.put(
                f"[{timestamp()}] Server exited with code {process.returncode}."
            )
            process = None
            running_port = None
            pending_items = []
            pending_last_update = None
            pending_error = None
            pending_scroll_offset = 0
            servers_items = []
            servers_last_update = None
            servers_error = None
            servers_scroll_offset = 0
            servers_expanded = {}
            servers_header_rects = []
            servers_cache = []
            servers_empty_streak = 0
            servers_last_send_seen = {}
            servers_blink_until = {}

        for event in pygame.event.get():
            if event.type == pygame.QUIT:
                running = False
                continue

            if event.type == pygame.MOUSEBUTTONDOWN:
                if port_box.collidepoint(event.pos):
                    port_active = True
                else:
                    port_active = False

                if toggle_button.hit(event.pos):
                    if process and process.poll() is None:
                        stop_process(process, log_queue)
                        pending_items = []
                        pending_last_update = None
                        pending_error = None
                        pending_scroll_offset = 0
                        clear_local_pending(local_pending_path)
                        servers_items = []
                        servers_last_update = None
                        servers_error = None
                        servers_scroll_offset = 0
                        clear_local_servers(local_servers_path)
                        servers_cache = []
                        servers_empty_streak = 0
                        servers_last_send_seen = {}
                        servers_blink_until = {}
                    elif validate_port(port_input):
                        process = start_process(port_input, log_queue, project_dir)
                        running_port = port_input
                    else:
                        log_queue.put(f"[{timestamp()}] Invalid port: {port_input}")

                if restart_button.hit(event.pos):
                    stop_process(process, log_queue)
                    if validate_port(port_input):
                        process = start_process(port_input, log_queue, project_dir)
                        running_port = port_input
                    else:
                        log_queue.put(f"[{timestamp()}] Invalid port: {port_input}")
                    pending_items = []
                    pending_last_update = None
                    pending_error = None
                    pending_scroll_offset = 0
                    clear_local_pending(local_pending_path)
                    servers_items = []
                    servers_last_update = None
                    servers_error = None
                    servers_scroll_offset = 0
                    clear_local_servers(local_servers_path)
                    servers_cache = []
                    servers_empty_streak = 0
                    servers_last_send_seen = {}
                    servers_blink_until = {}

                if clear_button.hit(event.pos):
                    log_lines.clear()
                    log_scroll_offset = 0

                if logs_tab.hit(event.pos):
                    active_tab = "logs"

                if pending_tab.hit(event.pos):
                    active_tab = "pending"

                if servers_tab.hit(event.pos):
                    active_tab = "servers"
                    servers_items, servers_last_update, servers_error = read_local_servers(
                        local_servers_path
                    )
                    last_servers_fetch = time.time()
                    if servers_items:
                        servers_cache = servers_items
                        servers_empty_streak = 0
                        servers_rows = build_server_rows(servers_items, servers_expanded)

                if active_tab == "logs" and not log_follow and bottom_button.hit(event.pos):
                    log_follow = True
                    log_scroll_offset = 0

                if active_tab == "pending":
                    for rect, request_id in pending_cancel_rects:
                        if rect.collidepoint(event.pos):
                            queue_local_cancel(local_cancel_path, request_id, log_queue)
                            break

                if active_tab == "servers":
                    for rect, ip in servers_header_rects:
                        if rect.collidepoint(event.pos):
                            servers_expanded[ip] = not servers_expanded.get(ip, True)
                            break

            if event.type == pygame.KEYDOWN and port_active:
                if event.key == pygame.K_RETURN:
                    port_active = False
                elif event.key == pygame.K_BACKSPACE:
                    port_input = port_input[:-1]
                else:
                    if event.unicode.isdigit() and len(port_input) < 5:
                        port_input += event.unicode

            if event.type == pygame.MOUSEWHEEL:
                if active_tab == "logs":
                    log_scroll_offset += event.y
                    max_offset = max(0, len(log_lines) - visible_lines)
                    log_scroll_offset = max(0, min(log_scroll_offset, max_offset))
                    log_follow = log_scroll_offset == 0
                elif active_tab == "pending":
                    pending_scroll_offset += event.y
                    max_offset = max(0, len(pending_items) - visible_lines)
                    pending_scroll_offset = max(0, min(pending_scroll_offset, max_offset))
                else:
                    servers_scroll_offset += event.y
                    max_offset = max(0, servers_total_rows - visible_lines)
                    servers_scroll_offset = max(0, min(servers_scroll_offset, max_offset))

        draw_vertical_gradient(
            screen,
            (0, 0, WINDOW_WIDTH, WINDOW_HEIGHT),
            COLORS["bg_top"],
            COLORS["bg_bottom"],
        )

        header = header_font.render("InfinityNet Control Panel", True, COLORS["text"])
        screen.blit(header, (24, 28))

        status_text = "Running" if process and process.poll() is None else "Stopped"
        status_color = COLORS["accent_green"] if status_text == "Running" else COLORS["accent_red"]
        status_label = ui_font.render(f"Status: {status_text}", True, status_color)
        screen.blit(status_label, (24, 56))

        port_label = ui_font.render("Port", True, COLORS["muted"])
        screen.blit(port_label, (port_box.x, port_box.y - 18))

        pygame.draw.rect(screen, (32, 36, 44), port_box, border_radius=8)
        pygame.draw.rect(screen, COLORS["panel_border"], port_box, 2, border_radius=8)
        port_text = ui_font.render(port_input or "", True, COLORS["text"])
        screen.blit(port_text, (port_box.x + 10, port_box.y + 10))

        running_label = (
            f"Running port: {running_port}" if running_port else "Running port: -"
        )
        running_text = ui_font.render(running_label, True, COLORS["muted"])
        screen.blit(running_text, (port_box.x, port_box.y + 46))

        is_running = process and process.poll() is None
        toggle_button.label = "Stop" if is_running else "Start"
        if is_running:
            toggle_button.style["bg"] = COLORS["accent_red"]
            toggle_button.style["border"] = (140, 60, 60)
        else:
            toggle_button.style["bg"] = COLORS["accent_green"]
            toggle_button.style["border"] = (35, 120, 80)

        mouse_pos = pygame.mouse.get_pos()
        toggle_button.draw(screen, ui_font, True, hovered=toggle_button.hit(mouse_pos))
        restart_button.draw(screen, ui_font, True, hovered=restart_button.hit(mouse_pos))
        clear_button.draw(screen, ui_font, True, hovered=clear_button.hit(mouse_pos))

        logs_tab.draw(
            screen,
            ui_font,
            True,
            active=active_tab == "logs",
            hovered=logs_tab.hit(mouse_pos),
        )
        pending_tab.draw(
            screen,
            ui_font,
            True,
            active=active_tab == "pending",
            hovered=pending_tab.hit(mouse_pos),
        )
        servers_tab.draw(
            screen,
            ui_font,
            True,
            active=active_tab == "servers",
            hovered=servers_tab.hit(mouse_pos),
        )
        if active_tab == "logs" and not log_follow:
            bottom_button.draw(screen, ui_font, True, hovered=bottom_button.hit(mouse_pos))

        draw_panel(screen, log_area)

        if active_tab == "logs":
            log_title = ui_font.render("Logs", True, COLORS["text"])
            screen.blit(log_title, (log_area.x + 8, log_area.y + 6))

            start_index = max(0, len(log_lines) - visible_lines - log_scroll_offset)
            visible = list(log_lines)[start_index : start_index + visible_lines]
            y = log_area.y + 28
            for line in visible:
                color = COLORS["text"]
                if " ERR:" in line or "ERR:" in line:
                    color = COLORS["accent_red"]
                elif " /send/" in line:
                    color = COLORS["accent_blue"]
                elif " /server/" in line:
                    color = COLORS["accent_orange"]
                elif "Started server.js" in line or "Listening on port" in line:
                    color = COLORS["accent_green"]
                elif "Stopping server.js" in line:
                    color = COLORS["accent_orange"]
                line_surf = mono_font.render(line, True, color)
                screen.blit(line_surf, (log_area.x + LOG_PADDING, y))
                y += LOG_LINE_HEIGHT
        elif active_tab == "pending":
            pending_title = ui_font.render("Pending Requests (Local)", True, COLORS["text"])
            screen.blit(pending_title, (log_area.x + 8, log_area.y + 6))

            status_line = "Last update: " + (pending_last_update or "-")
            status_color = (160, 200, 160) if pending_last_update else COLORS["muted"]
            status = ui_font.render(status_line, True, status_color)
            screen.blit(status, (log_area.x + LOG_PADDING, log_area.y + 32))

            if pending_error:
                err = ui_font.render(f"Error: {pending_error}", True, COLORS["accent_red"])
                screen.blit(err, (log_area.x + LOG_PADDING, log_area.y + 52))

            start_index = max(0, len(pending_items) - visible_lines - pending_scroll_offset)
            visible = pending_items[start_index : start_index + visible_lines]
            y = log_area.y + 80
            for entry in visible:
                source = entry.get("source") or "send"
                color = (100, 190, 240) if source == "send" else (240, 180, 90)
                endpoint = entry.get("endpoint") or "-"
                line = (
                    f'{entry.get("ip", "-")} {endpoint} '
                    f'[{entry.get("method", "-")}] '
                    f'id={entry.get("id", "-")[:8]}'
                )
                line_surf = mono_font.render(line, True, color)
                screen.blit(line_surf, (log_area.x + LOG_PADDING, y))

                cancel_rect = pygame.Rect(
                    log_area.right - 30,
                    y - 2,
                    20,
                    20,
                )
                pygame.draw.rect(screen, (160, 60, 60), cancel_rect, border_radius=4)
                pygame.draw.rect(screen, (220, 120, 120), cancel_rect, 2, border_radius=4)
                x_text = ui_font.render("X", True, COLORS["text"])
                x_rect = x_text.get_rect(center=cancel_rect.center)
                screen.blit(x_text, x_rect)
                pending_cancel_rects.append((cancel_rect, entry.get("id", "")))
                y += LOG_LINE_HEIGHT
        else:
            servers_title = ui_font.render("Servers (Last 3s)", True, COLORS["text"])
            screen.blit(servers_title, (log_area.x + 8, log_area.y + 6))

            status_line = "Last update: " + (servers_last_update or "-")
            status_color = (160, 200, 160) if servers_last_update else COLORS["muted"]
            status = ui_font.render(status_line, True, status_color)
            screen.blit(status, (log_area.x + LOG_PADDING, log_area.y + 32))

            if servers_error:
                err = ui_font.render(f"Error: {servers_error}", True, COLORS["accent_red"])
                screen.blit(err, (log_area.x + LOG_PADDING, log_area.y + 52))

            if not servers_items:
                empty = ui_font.render(
                    "No server activity in the last 3 seconds.",
                    True,
                    COLORS["muted"],
                )
                screen.blit(empty, (log_area.x + LOG_PADDING, log_area.y + 84))
                servers_header_rects = []
                pygame.display.flip()
                clock.tick(60)
                continue

            if not servers_rows:
                servers_rows = build_server_rows(servers_items, servers_expanded)

            now_ms = time.time() * 1000
            for row in servers_rows:
                if row.get("type") != "node":
                    continue
                if not row.get("isEndpoint"):
                    continue
                key = f"{row.get('ip')}|{row.get('path')}"
                last_send = row.get("lastSend", 0)
                if last_send and last_send > servers_last_send_seen.get(key, 0):
                    servers_last_send_seen[key] = last_send
                    servers_blink_until[key] = now_ms + BLINK_PULSE_MS

            start_index = max(0, len(servers_rows) - visible_lines - servers_scroll_offset)
            visible = servers_rows[start_index : start_index + visible_lines]
            y = log_area.y + 80
            servers_header_rects = []
            for item in visible:
                if item["type"] == "ip":
                    is_open = servers_expanded.get(item["ip"], True)
                    header_rect = pygame.Rect(
                        log_area.x + LOG_PADDING - 2,
                        y - 4,
                        log_area.width - LOG_PADDING * 2 + 4,
                        LOG_LINE_HEIGHT + 6,
                    )
                    pygame.draw.rect(
                        screen,
                        (40, 46, 58),
                        header_rect,
                        border_radius=6,
                    )
                    pygame.draw.rect(
                        screen,
                        (70, 78, 95),
                        header_rect,
                        2,
                        border_radius=6,
                    )
                    caret = "-" if is_open else "+"
                    caret_text = ui_font.render(caret, True, COLORS["muted"])
                    screen.blit(caret_text, (header_rect.x + 6, header_rect.y + 2))
                    ip_text = mono_font.render(item["text"], True, COLORS["accent_purple"])
                    screen.blit(ip_text, (header_rect.x + 22, header_rect.y + 2))
                    servers_header_rects.append((header_rect, item["ip"]))
                    y += LOG_LINE_HEIGHT + 4
                else:
                    indent = 18 * item["depth"]
                    pill_rect = pygame.Rect(
                        log_area.x + LOG_PADDING + 16 + indent,
                        y - 2,
                        log_area.width - LOG_PADDING * 2 - 30 - indent,
                        LOG_LINE_HEIGHT + 2,
                    )
                    key = f"{item.get('ip')}|{item.get('path')}"
                    is_recent_send = now_ms < servers_blink_until.get(key, 0)

                    base_color = (32, 40, 52)
                    border_color = (55, 70, 92)
                    text_color = (180, 210, 240)
                    if is_recent_send:
                        base_color = (60, 120, 200)
                        border_color = (90, 150, 220)
                        text_color = (235, 245, 255)

                    pygame.draw.rect(
                        screen,
                        base_color,
                        pill_rect,
                        border_radius=6,
                    )
                    pygame.draw.rect(
                        screen,
                        border_color,
                        pill_rect,
                        1,
                        border_radius=6,
                    )
                    ep_text = mono_font.render(item["text"], True, text_color)
                    screen.blit(ep_text, (pill_rect.x + 8, pill_rect.y + 2))
                    y += LOG_LINE_HEIGHT + 2

        pygame.display.flip()
        clock.tick(60)

    if process and process.poll() is None:
        stop_process(process, log_queue)
        time.sleep(0.1)

    pygame.quit()


if __name__ == "__main__":
    main()
