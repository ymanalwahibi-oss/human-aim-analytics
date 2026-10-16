
    pip install -r requirements.txt
    python human_aim.py



import cv2
import numpy as np
import mss
import time
import math
import os
import keyboard
import win32api
import win32gui
import win32con
import ctypes
from ctypes import wintypes
from threading import Thread, Lock
from queue import Queue, Empty
from collections import deque

# matplotlib بدون نافذة
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt


# ==================== الإعدادات ====================
SCREEN_W, SCREEN_H = 1920, 1080
FOV_SIZE = 400
FOV_LEFT = (SCREEN_W - FOV_SIZE) // 2
FOV_TOP = (SCREEN_H - FOV_SIZE) // 2
FOV_CENTER = FOV_SIZE // 2

COLOR_RANGES = {
    "red": [
        (np.array([0, 120, 120]), np.array([10, 255, 255])),
        (np.array([170, 120, 120]), np.array([180, 255, 255])),
    ],
    "blue": [
        (np.array([100, 120, 120]), np.array([140, 255, 255])),
    ],
    "green": [
        (np.array([40, 120, 120]), np.array([80, 255, 255])),
    ],
}

MIN_TARGET_AREA = 35
FLICK_SPEED_THRESHOLD = 3000
TRACKING_SPEED_THRESHOLD = 800
TRACKING_ERROR_THRESHOLD = 40
MAX_REALISTIC_SPEED = 50000
LIVE_REPORT_INTERVAL = 200
MAIN_LOOP_SLEEP = 0.001
SESSION_RESET_SLEEP = 0.4
LISTENER_STARTUP_SLEEP = 0.5
OUTPUT_DIR = "output"
REPORT_FILENAME = "human_aim_report.png"

os.makedirs(OUTPUT_DIR, exist_ok=True)

WM_INPUT = 0x00FF
RID_INPUT = 0x10000003
RIDEV_INPUTSINK = 0x00000100
RIM_TYPEMOUSE = 0
RAW_INPUT_CLASS_NAME = "HumanAimRawInputClass_v141"
RAW_INPUT_WINDOW_NAME = "HumanAimRawInputWindow"


# ==================== Windows Raw Input Structures ====================
class RAWINPUTHEADER(ctypes.Structure):
    _fields_ = [
        ("dwType", wintypes.DWORD),
        ("dwSize", wintypes.DWORD),
        ("hDevice", wintypes.HANDLE),
        ("wParam", wintypes.WPARAM),
    ]


class RAWMOUSE(ctypes.Structure):
    _fields_ = [
        ("usFlags", wintypes.USHORT),
        ("ulButtons", wintypes.ULONG),
        ("usButtonFlags", wintypes.USHORT),
        ("usButtonData", wintypes.USHORT),
        ("ulRawButtons", wintypes.ULONG),
        ("lLastX", wintypes.LONG),
        ("lLastY", wintypes.LONG),
        ("ulExtraInformation", wintypes.ULONG),
    ]


class RAWINPUT(ctypes.Structure):
    class _U(ctypes.Union):
        _fields_ = [("mouse", RAWMOUSE)]
    _fields_ = [("header", RAWINPUTHEADER), ("data", _U)]


class RAWINPUTDEVICE(ctypes.Structure):
    _fields_ = [
        ("usUsagePage", wintypes.USHORT),
        ("usUsage", wintypes.USHORT),
        ("dwFlags", wintypes.DWORD),
        ("hwndTarget", wintypes.HWND),
    ]


# ==================== الحالة المشتركة ====================
class SharedState:
    def __init__(self):
        self.lock = Lock()
        self.mouse_queue = Queue(maxsize=100000)
        self.session_active = False
        self.session_start_time = 0.0

    def start_session(self):
        with self.lock:
            self.session_start_time = time.perf_counter()
            self.session_active = True
            while not self.mouse_queue.empty():
                try:
                    self.mouse_queue.get_nowait()
                except:
                    break

    def stop_session(self):
        with self.lock:
            self.session_active = False

    def is_active(self):
        with self.lock:
            return self.session_active

    def get_start_time(self):
        with self.lock:
            return self.session_start_time


# ==================== مستمع Raw Input ====================
class RawInputListener:
    def __init__(self, state):
        self.state = state
        self._wndproc_ref = None
        self.hwnd = None
        self.thread = None
        self._running = False
        self._class_registered = False

    def start(self):
        self._running = True
        self.thread = Thread(target=self._run, daemon=True)
        self.thread.start()

    def stop(self):
        if self.hwnd:
            try:
                win32gui.PostMessage(self.hwnd, win32con.WM_CLOSE, 0, 0)
            except:
                pass
        if self.thread and self.thread.is_alive():
            self.thread.join(timeout=2.0)
        self._running = False

    def _run(self):
        WNDPROC = ctypes.WINFUNCTYPE(
            ctypes.c_int, wintypes.HWND, wintypes.UINT,
            wintypes.WPARAM, wintypes.LPARAM,
        )

        def wnd_proc(hwnd, msg, wparam, lparam):
            if msg == WM_INPUT:
                raw = RAWINPUT()
                size = wintypes.DWORD(ctypes.sizeof(raw))
                result = ctypes.windll.user32.GetRawInputData(
                    ctypes.c_void_p(lparam), RID_INPUT,
                    ctypes.byref(raw), ctypes.byref(size),
                    ctypes.c_uint(ctypes.sizeof(RAWINPUTHEADER)),
                )
                if result != 0 and result != 0xFFFFFFFF:
                    if raw.header.dwType == RIM_TYPEMOUSE:
                        dx = raw.data.mouse.lLastX
                        dy = raw.data.mouse.lLastY
                        if self.state.is_active() and (dx != 0 or dy != 0):
                            t = time.perf_counter() - self.state.get_start_time()
                            try:
                                self.state.mouse_queue.put_nowait((t, dx, dy))
                            except:
                                pass
            elif msg == win32con.WM_CLOSE:
                try:
                    win32gui.DestroyWindow(hwnd)
                except:
                    pass
                return 0
            return ctypes.windll.user32.DefWindowProcW(hwnd, msg, wparam, lparam)

        self._wndproc_ref = WNDPROC(wnd_proc)

        wc = win32gui.WNDCLASS()
        wc.lpfnWndProc = self._wndproc_ref
        wc.lpszClassName = RAW_INPUT_CLASS_NAME
        hinst = win32api.GetModuleHandle(None)
        wc.hInstance = hinst

        try:
            class_atom = win32gui.RegisterClass(wc)
            self._class_registered = True
        except:
            class_atom = RAW_INPUT_CLASS_NAME

        try:
            self.hwnd = win32gui.CreateWindow(
                class_atom, RAW_INPUT_WINDOW_NAME, 0,
                0, 0, 0, 0, 0, 0, hinst, None,
            )
        except Exception as e:
            print(f"[ERROR] CreateWindow: {e}")
            return

        rid = RAWINPUTDEVICE()
        rid.usUsagePage = 0x01
        rid.usUsage = 0x02
        rid.dwFlags = RIDEV_INPUTSINK
        rid.hwndTarget = self.hwnd

        if not ctypes.windll.user32.RegisterRawInputDevices(
            ctypes.byref(rid), 1, ctypes.sizeof(rid)
        ):
            print("[ERROR] RegisterRawInputDevices فشل")
            return

        print("[OK] Raw Input Listener يعمل")

        msg = wintypes.MSG()
        while self._running:
            try:
                ret = win32gui.GetMessage(ctypes.byref(msg), 0, 0, 0)
                if ret == 0:
                    break
                win32gui.TranslateMessage(ctypes.byref(msg))
                win32gui.DispatchMessage(ctypes.byref(msg))
            except:
                break

        try:
            if self._class_registered:
                win32gui.UnregisterClass(RAW_INPUT_CLASS_NAME, hinst)
        except:
            pass


# ==================== كشف الأهداف ====================
def find_multi_color_target(frame):
    """البحث عن هدف بأي لون مدعوم"""
    hsv = cv2.cvtColor(frame, cv2.COLOR_BGR2HSV)
    best_target = None
    best_area = MIN_TARGET_AREA

    for color_name, ranges in COLOR_RANGES.items():
        combined_mask = None
        for lower, upper in ranges:
            mask = cv2.inRange(hsv, lower, upper)
            combined_mask = mask if combined_mask is None else combined_mask + mask

        contours, _ = cv2.findContours(
            combined_mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE
        )
        for c in contours:
            area = cv2.contourArea(c)
            if area > best_area:
                best_area = area
                x, y, w, h = cv2.boundingRect(c)
                best_target = (x + w // 2, y + h // 2)

    return best_target


# ==================== محرك التحليل ====================
def classify_player(speeds, errors):
    """تصنيف نمط اللاعب بناءً على السرعة والدقة"""
    if not speeds:
        return "Unknown"
    max_speed = max(speeds)
    avg_error = np.mean(errors) if errors else 0

    if max_speed > FLICK_SPEED_THRESHOLD:
        return "Flick Shot"
    elif max_speed > TRACKING_SPEED_THRESHOLD and avg_error < TRACKING_ERROR_THRESHOLD:
        return "Continuous Tracking"
    return "Precision Adjusting"


def analyze_session(state, target_data, cursor_data):
    """التحليل الإحصائي الشامل للجلسة"""
    mouse_events = []
    while not state.mouse_queue.empty():
        try:
            mouse_events.append(state.mouse_queue.get_nowait())
        except Empty:
            break

    if len(mouse_events) < 5 or len(target_data) < 2:
        return None

    # حساب السرعات
    speeds = []
    last_time = None
    for t, dx, dy in mouse_events:
        if last_time is not None:
            dt = t - last_time
            if dt > 0:
                speed = math.sqrt(dx ** 2 + dy ** 2) / dt
                if speed < MAX_REALISTIC_SPEED:
                    speeds.append(speed)
        last_time = t

    # حساب الأخطاء
    errors = []
    if cursor_data:
        for t_t, tx, ty in target_data:
            closest = min(cursor_data, key=lambda e: abs(e[0] - t_t))
            _, cx, cy = closest
            error = math.sqrt((tx - cx) ** 2 + (ty - cy) ** 2)
            errors.append(error)

    if not speeds or not errors:
        return None

    return {
        "speeds": speeds,
        "errors": errors,
        "mouse_events_count": len(mouse_events),
        "targets_count": len(target_data),
        "avg_speed": float(np.mean(speeds)),
        "max_speed": float(max(speeds)),
        "avg_error": float(np.mean(errors)),
        "std_error": float(np.std(errors)),
        "median_error": float(np.median(errors)),
        "p95_error": float(np.percentile(errors, 95)),
        "player_style": classify_player(speeds, errors),
    }


# ==================== التقارير ====================
def print_console_stats(stats):
    """طباعة الإحصائيات في Terminal"""
    print(f"\n{'=' * 65}")
    print(f"[ANALYSIS] نمط اللاعب: {stats['player_style']}")
    print(f"{'=' * 65}")
    print(f"\nإحصائيات الجلسة:")
    print(f"   احداث الماوس:     {stats['mouse_events_count']:,}")
    print(f"   نقاط الاهداف:     {stats['targets_count']:,}")
    print(f"   متوسط السرعة:     {stats['avg_speed']:,.0f} px/s")
    print(f"   اقصى سرعة:        {stats['max_speed']:,.0f} px/s")
    print(f"   متوسط الخطا:      {stats['avg_error']:.2f} px")
    print(f"   انحراف الخطا:     {stats['std_error']:.2f} px")
    print(f"   الوسيط:           {stats['median_error']:.2f} px")
    print(f"   P95:              {stats['p95_error']:.2f} px")


def generate_chart_report(stats):
    """توليد تقرير بياني PNG"""
    plt.figure(figsize=(12, 5))

    # الرسم 1: منحنى السرعة
    plt.subplot(1, 2, 1)
    plt.plot(stats["speeds"], color='mediumpurple', alpha=0.8, linewidth=0.8)
    plt.axhline(stats["avg_speed"], color='red', linestyle='--',
                label=f"Avg: {stats['avg_speed']:.0f}")
    plt.title(f'Speed Curve ({stats["player_style"]})', fontsize=11)
    plt.xlabel('Event Ticks')
    plt.ylabel('Velocity (px/sec)')
    plt.legend()
    plt.grid(True, linestyle=':', alpha=0.5)

    # الرسم 2: توزيع الأخطاء
    plt.subplot(1, 2, 2)
    plt.hist(stats["errors"], bins=25, color='orange',
             alpha=0.7, edgecolor='black')
    plt.axvline(stats["avg_error"], color='red', linestyle='--',
                label=f"Mean: {stats['avg_error']:.1f}px")
    plt.axvline(stats["p95_error"], color='darkred', linestyle=':',
                label=f"P95: {stats['p95_error']:.1f}px")
    plt.title('Error Distribution', fontsize=11)
    plt.xlabel('Distance Error (Pixels)')
    plt.ylabel('Frequency')
    plt.legend()
    plt.grid(True, linestyle=':', alpha=0.5)

    plt.tight_layout()
    filepath = os.path.join(OUTPUT_DIR, REPORT_FILENAME)
    plt.savefig(filepath, dpi=300, bbox_inches='tight')
    plt.close()
    return filepath


def save_text_report(stats, filepath):
    """حفظ تقرير نصي"""
    txt_path = filepath.replace(".png", ".txt")
    with open(txt_path, "w", encoding="utf-8") as f:
        f.write("HumanAim Analytics - Session Report\n")
        f.write("=" * 50 + "\n\n")
        f.write(f"Player Style: {stats['player_style']}\n\n")
        f.write(f"Mouse Events:    {stats['mouse_events_count']:,}\n")
        f.write(f"Targets:         {stats['targets_count']:,}\n")
        f.write(f"Avg Speed:       {stats['avg_speed']:,.0f} px/s\n")
        f.write(f"Max Speed:       {stats['max_speed']:,.0f} px/s\n")
        f.write(f"Avg Error:       {stats['avg_error']:.2f} px\n")
        f.write(f"Std Error:       {stats['std_error']:.2f} px\n")
        f.write(f"Median Error:    {stats['median_error']:.2f} px\n")
        f.write(f"P95 Error:       {stats['p95_error']:.2f} px\n")
    return txt_path


# ==================== البرنامج الرئيسي ====================
def main():
    print("=" * 65)
    print("HumanAim Analytics Platform v1.4.1")
    print("=" * 65)
    print("F8  : بدء جلسة تحليل جديدة")
    print("ESC : إنهاء وحفظ التقرير")
    print("=" * 65)
    print("هذه الأداة للتحليل فقط - لا تتحكم في اللعبة")
    print("=" * 65)

    state = SharedState()
    listener = RawInputListener(state)
    listener.start()
    time.sleep(LISTENER_STARTUP_SLEEP)

    target_data = []
    cursor_data = []
    frame_counter = 0
    frame_times = deque(maxlen=60)

    monitor = {
        "top": FOV_TOP, "left": FOV_LEFT,
        "width": FOV_SIZE, "height": FOV_SIZE,
    }

    try:
        with mss.mss() as sct:
            while True:
                # فحص الخروج
                if keyboard.is_pressed('esc'):
                    state.stop_session()
                    print("\n[INFO] انهاء البرنامج...")
                    break

                # فحص البدء
                if keyboard.is_pressed('f8'):
                    if not state.is_active():
                        state.start_session()
                        target_data.clear()
                        cursor_data.clear()
                        frame_counter = 0
                        frame_times.clear()
                        print("\n[SESSION STARTED] ابدأ التدريب!")
                        time.sleep(SESSION_RESET_SLEEP)

                if not state.is_active():
                    time.sleep(0.05)
                    continue

                # حلقة القياس
                frame_start = time.perf_counter()
                frame_counter += 1
                t_frame = frame_start - state.get_start_time()

                # التقاط الشاشة
                screenshot = sct.grab(monitor)
                frame = np.array(screenshot)
                frame = cv2.cvtColor(frame, cv2.COLOR_BGRA2BGR)

                # قراءة المؤشر
                try:
                    cx, cy = win32api.GetCursorPos()
                    rel_cx = cx - FOV_LEFT
                    rel_cy = cy - FOV_TOP
                    cursor_data.append((t_frame, rel_cx, rel_cy))
                except:
                    rel_cx, rel_cy = FOV_CENTER, FOV_CENTER

                # كشف الهدف
                target = find_multi_color_target(frame)
                current_error = None
                if target:
                    tx, ty = target
                    target_data.append((t_frame, tx, ty))
                    current_error = math.sqrt((tx - rel_cx) ** 2 + (ty - rel_cy) ** 2)

                # FPS
                frame_times.append(time.perf_counter() - frame_start)

                # تقرير حي
                if frame_counter % LIVE_REPORT_INTERVAL == 0 and frame_times:
                    avg_ft = sum(frame_times) / len(frame_times)
                    fps = 1.0 / avg_ft if avg_ft > 0 else 0
                    err_str = f"{current_error:6.1f}" if current_error is not None else "  --- "
                    print(
                        f"\rFPS:{fps:6.1f} | Err:{err_str}px | "
                        f"T:{len(target_data):5d} | "
                        f"E:{state.mouse_queue.qsize():5d} | "
                        f"{t_frame:6.1f}s",
                        end=""
                    )

                time.sleep(MAIN_LOOP_SLEEP)

    except KeyboardInterrupt:
        print("\n[INFO] Ctrl+C")

    finally:
        state.stop_session()
        print("\n\n[INFO] جاري التحليل...")
        listener.stop()
        time.sleep(0.2)

        stats = analyze_session(state, target_data, cursor_data)

        if stats:
            print_console_stats(stats)
            filepath = generate_chart_report(stats)
            save_text_report(stats, filepath)
            print(f"\n[SUCCESS] التقرير: {filepath}")
        else:
            print("[INFO] بيانات غير كافية للتقرير")
            print(f"       احداث الماوس: {state.mouse_queue.qsize()}")
            print(f"       نقاط الاهداف: {len(target_data)}")

        print("[INFO] تم اغلاق كل الموارد")


if __name__ == "__main__":
    main()
