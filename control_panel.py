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
ENDPOINT_REFRESH_SEC = 0.8
SERVERS_WINDOW_MS = 5000
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

def read_local_endpoint_stats(path):
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
        return data.get("endpoints", []), data.get("updatedAt"), None
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

def wrap_text(text, font, max_width):
    words = text.split()
    if not words:
        return [""]
    lines = []
    current = words[0]
    for word in words[1:]:
        trial = f"{current} {word}"
        if font.size(trial)[0] <= max_width:
            current = trial
        else:
            lines.append(current)
            current = word
    lines.append(current)
    return lines

def normalize_site_input(text):
    return text.strip().lower().rstrip(".")

def normalize_endpoint_input(text):
    return text.strip().strip("/")

def build_endpoint_info_lines(endpoints, site_input, endpoint_input):
    site = normalize_site_input(site_input)
    endpoint_raw = endpoint_input.strip()
    endpoint = normalize_endpoint_input(endpoint_input)
    lines = []

    if not site or endpoint_raw == "":
        lines.append("Enter a site name and endpoint to view stats.")
        return lines

    match = None
    for item in endpoints:
        server_name = item.get("serverName") or ""
        item_site = normalize_site_input(server_name)
        item_endpoint = normalize_endpoint_input(item.get("endpoint") or "")
        if item_site == site and item_endpoint == endpoint:
            match = item
            break

    if not match:
        lines.append("No data recorded for this site and endpoint yet.")
        return lines

    total_requests = match.get("totalRequests", 0) or 0
    method_counts = match.get("methodCounts") or {}
    get_count = method_counts.get("GET", 0) or 0
    post_count = method_counts.get("POST", 0) or 0
    total_responses = match.get("totalResponses", 0) or 0
    avg_delay = match.get("avgResponseDelayMs")
    last_delay = match.get("lastResponseDelayMs")
    last_request_at = match.get("lastRequestAt") or "-"
    last_response_at = match.get("lastResponseAt") or "-"

    response_rate = 0.0
    if total_requests > 0:
        response_rate = (total_responses / total_requests) * 100.0

    lines.append(f"Site: {site}")
    display_endpoint = "/" if endpoint == "" else f"/{endpoint}"
    lines.append(f"Endpoint: {display_endpoint}")
    lines.append(f"Total requests: {total_requests}")
    lines.append(f"Methods: GET {get_count} | POST {post_count}")
    lines.append(f"Responses: {total_responses} ({response_rate:.1f}%)")
    lines.append(f"Last request: {last_request_at}")
    lines.append(f"Last response: {last_response_at}")
    if avg_delay is None:
        lines.append("Avg response delay: -")
    else:
        lines.append(f"Avg response delay: {int(avg_delay)} ms")
    if last_delay is None:
        lines.append("Last response delay: -")
    else:
        lines.append(f"Last response delay: {int(last_delay)} ms")

    lines.append("")
    lines.append("Queries:")
    queries = match.get("queries") or []
    if not queries:
        lines.append("  (no queries recorded)")
        return lines

    sorted_queries = sorted(queries, key=lambda q: q.get("count", 0), reverse=True)
    for query in sorted_queries:
        count = query.get("count", 0) or 0
        percent = (count / total_requests * 100.0) if total_requests > 0 else 0.0
        label = query.get("query") or "(no query)"
        display_label = "(no query)"
        if label != "(no query)":
            keys = []
            for part in label.split("&"):
                key = part.split("=", 1)[0].strip()
                if key and key not in keys:
                    keys.append(key)
            display_label = ", ".join(keys) if keys else "(no query)"
        lines.append(f"  {display_label} — {count} ({percent:.1f}%)")

    return lines

def estimate_server_rows(servers_rows, font, max_width):
    total = 0
    for row in servers_rows:
        if row.get("type") == "desc":
            lines = wrap_text(row.get("text", ""), font, max_width)
            total += max(1, len(lines))
        else:
            total += 1
    return total

def build_endpoint_tree(endpoints):
    root = {
        "children": {},
        "lastSend": 0,
        "isEndpoint": False,
        "delayMs": None,
        "sumDelay": 0,
        "countDelay": 0,
    }
    for endpoint in endpoints:
        path = endpoint.get("endpoint") or ""
        last_send = endpoint.get("lastSend") or 0
        last_delay = endpoint.get("lastDelayMs")
        parts = [part for part in path.split("/") if part]
        if not parts:
            parts = ["(root)"]
        node = root
        for part in parts:
            if part not in node["children"]:
                node["children"][part] = {
                    "children": {},
                    "lastSend": 0,
                    "isEndpoint": False,
                    "delayMs": None,
                    "sumDelay": 0,
                    "countDelay": 0,
                }
            node = node["children"][part]
        node["lastSend"] = max(node["lastSend"], last_send)
        node["isEndpoint"] = True
        if isinstance(last_delay, (int, float)):
            if node["delayMs"] is None:
                node["delayMs"] = last_delay
            else:
                node["delayMs"] = max(node["delayMs"], last_delay)
            node["sumDelay"] += last_delay
            node["countDelay"] += 1
    def aggregate(node):
        for child in node["children"].values():
            aggregate(child)
            node["sumDelay"] += child.get("sumDelay", 0)
            node["countDelay"] += child.get("countDelay", 0)
        return node

    aggregate(root)
    return root

def flatten_endpoint_tree(node, depth, rows, prefix, ip):
    for name in sorted(node["children"].keys()):
        child = node["children"][name]
        has_children = len(child["children"]) > 0
        full_path = name if not prefix else f"{prefix}/{name}"
        avg_delay = None
        if child.get("countDelay", 0) > 0:
            avg_delay = child["sumDelay"] / child["countDelay"]
        rows.append(
            {
                "type": "node",
                "text": name,
                "depth": depth,
                "path": full_path,
                "ip": ip,
                "lastSend": child.get("lastSend", 0),
                "delayMs": child.get("delayMs", None),
                "avgDelayMs": avg_delay,
                "isEndpoint": child.get("isEndpoint", False),
                "hasChildren": has_children,
            }
        )
        flatten_endpoint_tree(child, depth + 1, rows, full_path, ip)

def build_server_rows(servers_items, servers_expanded):
    rows = []
    for server in servers_items:
        ip = server.get("ip", "-")
        name = server.get("serverName") if isinstance(server.get("serverName"), str) else "Unknown"
        label = f"{name} ({ip})"
        rows.append(
            {
                "type": "ip",
                "ip": ip,
                "text": label,
                "active": bool(server.get("active", False)),
            }
        )
        description = server.get("description")
        if isinstance(description, str) and description.strip():
            rows.append(
                {
                    "type": "desc",
                    "ip": ip,
                    "text": description.strip(),
                }
            )
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
    endpoint_items = []
    endpoint_last_update = None
    endpoint_error = None
    last_endpoint_fetch = 0.0
    endpoint_scroll_offset = 0
    endpoint_info_site = ""
    endpoint_info_endpoint = ""
    endpoint_active_field = None
    endpoint_lines = []

    process = None
    running_port = None

    local_pending_path = os.path.join(project_dir, "local_pending.json")
    local_cancel_path = os.path.join(project_dir, "local_cancel.jsonl")
    local_servers_path = os.path.join(project_dir, "local_servers.json")
    local_endpoint_stats_path = os.path.join(project_dir, "local_endpoint_stats.json")

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
    endpoint_tab = Button(
        (SIDE_MARGIN + 370, TAB_ROW_Y, 160, 28),
        "Endpoint Info",
        {"bg": (40, 50, 62), "border": COLORS["panel_border"], "text": COLORS["text"]},
    )
    bottom_button = Button(
        (SIDE_MARGIN + 570, TAB_ROW_Y, 120, 28),
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
        endpoint_content_top = log_area.y + 140
        endpoint_visible_lines = max(
            1, (log_area.bottom - endpoint_content_top - 8) // LOG_LINE_HEIGHT
        )
        endpoint_site_rect = pygame.Rect(
            log_area.x + LOG_PADDING,
            log_area.y + 70,
            log_area.width - LOG_PADDING * 2,
            32,
        )
        endpoint_endpoint_rect = pygame.Rect(
            log_area.x + LOG_PADDING,
            log_area.y + 110,
            log_area.width - LOG_PADDING * 2,
            32,
        )
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
            desc_width = log_area.width - LOG_PADDING * 2 - 30
            servers_total_rows = estimate_server_rows(servers_rows, ui_font, desc_width)
            max_servers_offset = max(0, servers_total_rows - visible_lines)
            servers_scroll_offset = max(0, min(servers_scroll_offset, max_servers_offset))

        if active_tab == "endpoint":
            now = time.time()
            if now - last_endpoint_fetch > ENDPOINT_REFRESH_SEC:
                last_endpoint_fetch = now
                endpoint_items, endpoint_last_update, endpoint_error = read_local_endpoint_stats(
                    local_endpoint_stats_path
                )
            endpoint_lines = build_endpoint_info_lines(
                endpoint_items, endpoint_info_site, endpoint_info_endpoint
            )
            max_endpoint_offset = max(0, len(endpoint_lines) - endpoint_visible_lines)
            endpoint_scroll_offset = max(
                0, min(endpoint_scroll_offset, max_endpoint_offset)
            )

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
                if active_tab == "endpoint":
                    if endpoint_site_rect.collidepoint(event.pos):
                        endpoint_active_field = "site"
                    elif endpoint_endpoint_rect.collidepoint(event.pos):
                        endpoint_active_field = "endpoint"
                    else:
                        endpoint_active_field = None
                else:
                    endpoint_active_field = None

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

                if endpoint_tab.hit(event.pos):
                    active_tab = "endpoint"
                    endpoint_items, endpoint_last_update, endpoint_error = read_local_endpoint_stats(
                        local_endpoint_stats_path
                    )
                    last_endpoint_fetch = time.time()
                    endpoint_scroll_offset = 0

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
            elif event.type == pygame.KEYDOWN and active_tab == "endpoint" and endpoint_active_field:
                if event.key == pygame.K_RETURN:
                    endpoint_active_field = None
                elif event.key == pygame.K_TAB:
                    endpoint_active_field = (
                        "endpoint" if endpoint_active_field == "site" else "site"
                    )
                elif event.key == pygame.K_BACKSPACE:
                    if endpoint_active_field == "site":
                        endpoint_info_site = endpoint_info_site[:-1]
                    else:
                        endpoint_info_endpoint = endpoint_info_endpoint[:-1]
                    endpoint_scroll_offset = 0
                else:
                    if event.unicode and len(event.unicode) == 1 and ord(event.unicode) >= 32:
                        if endpoint_active_field == "site" and len(endpoint_info_site) < 120:
                            endpoint_info_site += event.unicode
                            endpoint_scroll_offset = 0
                        elif (
                            endpoint_active_field == "endpoint"
                            and len(endpoint_info_endpoint) < 160
                        ):
                            endpoint_info_endpoint += event.unicode
                            endpoint_scroll_offset = 0

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
                    if active_tab == "servers":
                        servers_scroll_offset += event.y
                        max_offset = max(0, servers_total_rows - visible_lines)
                        servers_scroll_offset = max(
                            0, min(servers_scroll_offset, max_offset)
                        )
                    elif active_tab == "endpoint":
                        endpoint_scroll_offset += event.y
                        max_offset = max(0, len(endpoint_lines) - endpoint_visible_lines)
                        endpoint_scroll_offset = max(
                            0, min(endpoint_scroll_offset, max_offset)
                        )

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
        endpoint_tab.draw(
            screen,
            ui_font,
            True,
            active=active_tab == "endpoint",
            hovered=endpoint_tab.hit(mouse_pos),
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
        elif active_tab == "servers":
            servers_title = ui_font.render("Servers (Last 5s)", True, COLORS["text"])
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
                    "No server activity in the last 5 seconds.",
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
                    is_active = item.get("active", False)
                    header_bg = (40, 46, 58) if is_active else (32, 34, 40)
                    header_border = (70, 78, 95) if is_active else (80, 60, 60)
                    pygame.draw.rect(screen, header_bg, header_rect, border_radius=6)
                    pygame.draw.rect(screen, header_border, header_rect, 2, border_radius=6)
                    caret = "-" if is_open else "+"
                    caret_text = ui_font.render(caret, True, COLORS["muted"])
                    screen.blit(caret_text, (header_rect.x + 6, header_rect.y + 2))
                    ip_text = mono_font.render(item["text"], True, COLORS["accent_purple"])
                    screen.blit(ip_text, (header_rect.x + 22, header_rect.y + 2))
                    if not is_active:
                        inactive_text = ui_font.render("inactive", True, COLORS["accent_red"])
                        inactive_rect = inactive_text.get_rect(
                            right=header_rect.right - 10, centery=header_rect.centery
                        )
                        screen.blit(inactive_text, inactive_rect)
                    servers_header_rects.append((header_rect, item["ip"]))
                    y += LOG_LINE_HEIGHT + 4
                elif item["type"] == "desc":
                    desc_width = log_area.width - LOG_PADDING * 2 - 30
                    lines = wrap_text(item["text"], ui_font, desc_width - 16)
                    box_height = max(1, len(lines)) * LOG_LINE_HEIGHT + 6
                    desc_rect = pygame.Rect(
                        log_area.x + LOG_PADDING + 18,
                        y - 2,
                        desc_width,
                        box_height,
                    )
                    pygame.draw.rect(screen, (28, 34, 44), desc_rect, border_radius=6)
                    pygame.draw.rect(screen, (50, 60, 78), desc_rect, 1, border_radius=6)
                    text_y = desc_rect.y + 4
                    for line in lines:
                        desc_text = ui_font.render(line, True, COLORS["muted"])
                        screen.blit(desc_text, (desc_rect.x + 8, text_y))
                        text_y += LOG_LINE_HEIGHT
                    y += box_height + 4
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
                    label = item["text"]
                    if item.get("hasChildren") and item.get("avgDelayMs") is not None:
                        label = f"{label} ({int(item['avgDelayMs'])} ms avg)"
                    elif not item.get("hasChildren") and item.get("delayMs") is not None:
                        label = f"{label} ({int(item['delayMs'])} ms)"
                    ep_text = mono_font.render(label, True, text_color)
                    screen.blit(ep_text, (pill_rect.x + 8, pill_rect.y + 2))
                    y += LOG_LINE_HEIGHT + 2
        else:
            endpoint_title = ui_font.render("Endpoint Info", True, COLORS["text"])
            screen.blit(endpoint_title, (log_area.x + 8, log_area.y + 6))

            status_line = "Last update: " + (endpoint_last_update or "-")
            status_color = (160, 200, 160) if endpoint_last_update else COLORS["muted"]
            status = ui_font.render(status_line, True, status_color)
            screen.blit(status, (log_area.x + LOG_PADDING, log_area.y + 32))

            if endpoint_error:
                err = ui_font.render(f"Error: {endpoint_error}", True, COLORS["accent_red"])
                screen.blit(err, (log_area.x + LOG_PADDING, log_area.y + 52))

            site_label = ui_font.render("Site name", True, COLORS["muted"])
            screen.blit(site_label, (endpoint_site_rect.x, endpoint_site_rect.y - 18))
            endpoint_label = ui_font.render("Endpoint", True, COLORS["muted"])
            screen.blit(
                endpoint_label, (endpoint_endpoint_rect.x, endpoint_endpoint_rect.y - 18)
            )

            def draw_input(rect, value, is_active):
                bg = (38, 44, 54) if is_active else (30, 34, 42)
                border = COLORS["accent_blue"] if is_active else COLORS["panel_border"]
                pygame.draw.rect(screen, bg, rect, border_radius=6)
                pygame.draw.rect(screen, border, rect, 2, border_radius=6)
                text_surface = mono_font.render(value, True, COLORS["text"])
                screen.blit(text_surface, (rect.x + 8, rect.y + 7))

            draw_input(endpoint_site_rect, endpoint_info_site, endpoint_active_field == "site")
            draw_input(
                endpoint_endpoint_rect,
                endpoint_info_endpoint,
                endpoint_active_field == "endpoint",
            )

            y = endpoint_content_top
            start_index = max(
                0, len(endpoint_lines) - endpoint_visible_lines - endpoint_scroll_offset
            )
            visible = endpoint_lines[start_index : start_index + endpoint_visible_lines]
            for line in visible:
                color = COLORS["text"]
                if line.startswith("No data") or line.startswith("Enter"):
                    color = COLORS["muted"]
                elif line.startswith("Queries"):
                    color = COLORS["accent_blue"]
                elif line.startswith("Site:") or line.startswith("Endpoint:"):
                    color = COLORS["accent_purple"]
                elif line.startswith("Responses:") and "0 (" in line:
                    color = COLORS["accent_orange"]
                text_surf = mono_font.render(line, True, color)
                screen.blit(text_surf, (log_area.x + LOG_PADDING, y))
                y += LOG_LINE_HEIGHT

        pygame.display.flip()
        clock.tick(60)

    if process and process.poll() is None:
        stop_process(process, log_queue)
        time.sleep(0.1)

    pygame.quit()


if __name__ == "__main__":
    main()
