import json
import logging
import re
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Optional

import cv2
import numpy as np
from fuzzywuzzy import process as fzwzprocess
from maa.context import Context
from maa.controller import AdbController
from maa.custom_action import CustomAction
from maa.custom_recognition import CustomRecognition
from maa.resource import Resource
from maa.tasker import Tasker
from maa.toolkit import AdbDevice, Toolkit
from minitouchpy import (
    MNT,
    MNTEvATive7LogEventData,
    MNTEvent,
    MNTEventData,
    MNTServerCommunicateType,
)

import player
from api import BestdoriAPI
from chart import Chart
from util import get_color_eval_in_range, get_runtime_info


def resource_path(relative_path):
    """
    Get the absolute path to a resource, compatible with both development environments
    (including the 'src' directory layout) and PyInstaller-packaged environments.
    """
    if getattr(sys, 'frozen', False):
        # When running in a packaged bundle, the base path is the temporary folder created by PyInstaller.
        base_path = Path(sys._MEIPASS)
    else:
        # In a development environment, trace up one level from the current file's location (__file__)
        # to the project root.
        base_path = Path(__file__).parent.parent

    return base_path / relative_path


# --- Global Variables & Constants ---
PHOTOGATE_LATENCY = 30
DEFAULT_MOVE_SLICE_SIZE = 10
CMD_SLICE_SIZE = 100
STABLE_THRESHOLD = 3
CONSECUTIVE_FRAMES_NEEDED = 120
FREEZE_SLEEP_TIME = 0.005
CONFIDENCE_THRESHOLD_FAILURE = 0.8
CONFIDENCE_THRESHOLD_PLAY = 0.9
DIFFICULTY = "hard"
HUMAN_DELAY_ENABLED = False
IS_FULL_SONG = False
IS_HIGH_DIFFICULTY = False
OFFSET = {"up": 0, "down": 0, "move": 0, "wait": 0.0, "interval": 0.0}
IS_INITIALISED = False  # <-- 新增：全局初始化状态标志
MANUAL_SONG_NAME: Optional[str] = None  # 手动指定的歌曲名
MANUAL_CHART_FILE: Optional[Path] = None  # 手动指定的谱面文件路径

# Playback monitor thread
stop_event = threading.Event()
playback_started_event = threading.Event()

# --- MAA & System Components ---

maaresource = Resource()
maatasker = Tasker()
maacontroller: Optional[AdbController] = None
device: Optional[AdbDevice] = None
current_player: Optional[player.Player] = None
mnt: Optional[MNT] = None

# --- Song & Chart Data ---
all_song_name_indexes: dict[str, str] = {
    list(filter(lambda title: title is not None, sinfo["musicTitle"]))[0]: sid
    for sid, sinfo in BestdoriAPI.get_song_list().items()
}
current_song_name: Optional[str] = None
current_song_id: Optional[str] = None
current_chart: Optional[Chart] = None
current_orientation: int = 0

# --- Latency Compensation & Real-time Data ---
callback_data: dict = {}
callback_data_lock = threading.Lock()

def load_song_by_name(song_name: str):
    """直接通过歌名加载歌曲，跳过OCR识别"""
    global current_song_name, current_song_id, current_chart, current_orientation
    
    # 模糊匹配歌名
    match = fuzzy_match_song(song_name)
    if not match:
        logging.error(f"未找到歌曲: {song_name}")
        return False
    
    matched_name, confidence = match
    if confidence < 60:
        logging.warning(f"歌名匹配置信度较低: {confidence}%, 匹配结果: {matched_name}")
    
    current_song_name = matched_name
    current_song_id = all_song_name_indexes[current_song_name]
    current_chart = Chart((current_song_id, DIFFICULTY), current_song_name)
    current_chart.notes_to_actions(current_player.resolution, DEFAULT_MOVE_SLICE_SIZE, humanize=HUMAN_DELAY_ENABLED)
    current_orientation = _get_orientation()
    current_chart.actions_to_MNTcmd(
        (mnt.max_x, mnt.max_y), current_orientation, OFFSET, CMD_SLICE_SIZE
    )
    logging.info(f"手动加载歌曲: {current_song_name} (ID: {current_song_id})")
    return True


def load_chart_from_file(file_path):
    """从JSON文件直接加载谱面"""
    global current_song_name, current_song_id, current_chart, current_orientation
    
    if isinstance(file_path, str):
        file_path = Path(file_path)
    
    if not file_path.exists():
        logging.error(f"谱面文件不存在: {file_path}")
        return False
    
    try:
        with open(file_path, 'r', encoding='utf-8-sig') as f:
            chart_data = json.load(f)
        
        current_song_name = file_path.stem
        current_song_id = "custom"
        current_orientation = _get_orientation()
        
        # 创建Chart对象并手动初始化必要属性
        from chart import Chart
        current_chart = object.__new__(Chart)
        current_chart._id_ = current_song_id
        current_chart._difficulty = DIFFICULTY
        current_chart._song_name = current_song_name
        current_chart._chart_data = chart_data
        current_chart._logger = logging.getLogger(f"{current_song_id}-{DIFFICULTY}")
        current_chart._bpms = []
        current_chart.actions = []
        current_chart._commands = []
        current_chart._total = len(chart_data)
        current_chart.actions_to_cmd_index = 0
        current_chart._a2c_offset = 0
        current_chart._a2c_rounded_loss = 0.0
        
        # 手动调用 _process_time_chart 来处理 beat -> time 转换
        current_chart._process_time_chart()
        
        # 调用后续方法
        current_chart.notes_to_actions(current_player.resolution, DEFAULT_MOVE_SLICE_SIZE, humanize=HUMAN_DELAY_ENABLED)
        current_chart.actions_to_MNTcmd(
            (mnt.max_x, mnt.max_y), current_orientation, OFFSET, CMD_SLICE_SIZE
        )
        logging.info(f"从文件加载谱面: {current_song_name} ({file_path})")
        return True
    except Exception as e:
        logging.error(f"加载谱面文件失败: {e}")
        import traceback
        logging.error(traceback.format_exc())
        return False
        
def reset_callback_data():
    global callback_data
    callback_data = {
        "wait": {"total": 0, "total_offset": 0.0},
        "move": {"uncommited": 0, "total": 0, "total_offset": 0.0},
        "up": {"uncommited": 0, "total": 0, "total_offset": 0.0},
        "down": {"uncommited": 0, "total": 0, "total_offset": 0.0},
        "interval": {"total": 0, "total_offset": 0.0},
        "last_cmd_endtime": -1,
    }


reset_callback_data()


def fuzzy_match_song(name):
    return fzwzprocess.extractOne(name, list(all_song_name_indexes.keys()))


def _get_orientation():
    try:
        creationflags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
        output = subprocess.check_output(
            [
                str(device.adb_path.absolute()),
                "-s",
                device.address,
                "shell",
                "dumpsys input|grep SurfaceOrientation",
            ],
            text=True,
            creationflags=creationflags,
        )
        match = re.search(r"SurfaceOrientation:\s*(\d+)", output)
        return int(match.group(1))
    except Exception as e:
        logging.error(f"Failed to get SurfaceOrientation: {e}")
        return 0


def save_song(name):
    global current_song_name, current_song_id, current_chart, current_orientation
    current_song_name = name
    current_song_id = all_song_name_indexes[current_song_name]
    current_chart = Chart((current_song_id, DIFFICULTY), current_song_name)
    current_chart.notes_to_actions(current_player.resolution, DEFAULT_MOVE_SLICE_SIZE, humanize=HUMAN_DELAY_ENABLED)
    current_orientation = _get_orientation()
    current_chart.actions_to_MNTcmd(
        (mnt.max_x, mnt.max_y), current_orientation, OFFSET, CMD_SLICE_SIZE
    )
    logging.info(f"Saved song: {name}")


def get_scaled_template(template_path):
    template = cv2.imread(template_path, 0)
    runtime_h, runtime_w, _ = current_player.ipc_capture_display().shape
    scale_factor = runtime_w / 1920
    if np.isclose(scale_factor, 1.0):
        return template
    original_h, original_w = template.shape[:2]
    new_w = int(original_w * scale_factor)
    new_h = int(original_h * scale_factor)
    if new_w < 1 or new_h < 1:
        return template
    resized_template = cv2.resize(template, (new_w, new_h), interpolation=cv2.INTER_AREA)
    return resized_template


def monitor_failure_thread(stop_event, playback_started_event):
    """
    A background monitoring thread.
    It waits for the playback start signal, then continuously monitors for the "Live Failed" screen through image matching.
    """
    try:
        logging.info("Monitor thread started, waiting for playback start signal.")

        # Wait for "playback started" signal from play_song function, timeout after 60s
        playback_started_event.wait(timeout=30)

        if not playback_started_event.is_set():
            logging.warning("Timeout waiting for playback start signal, monitor thread exiting.")
            stop_event.set()
            return

        logging.info("Received playback start signal, starting screen monitoring.")

        # Load template image once for efficiency
        # Note: Please ensure this path matches your project resource path
        fail_template_path = resource_path("assets/resource/image/live/live_failed.png")
        if not fail_template_path.exists():
            logging.error(
                f"Live Failed template image not found: {fail_template_path}, monitor thread cannot work.")
            stop_event.set()
            return

        template = get_scaled_template(fail_template_path)

        # Monitor loop until stop signal received
        while not stop_event.is_set():
            screen_bgr = current_player.ipc_capture_display()
            if screen_bgr is None:
                time.sleep(1)
                continue

            screen_gray = cv2.cvtColor(screen_bgr, cv2.COLOR_BGR2GRAY)

            # Perform template matching
            result = cv2.matchTemplate(screen_gray, template, cv2.TM_CCOEFF_NORMED)
            _, max_val, _, _ = cv2.minMaxLoc(result)

            if max_val >= CONFIDENCE_THRESHOLD_FAILURE:
                logging.error(f"Detected 'Live Failed' screen (match: {max_val:.2f})! Sending stop signal!")
                stop_event.set()  # Key: Set stop event to notify other threads
                break  # Task complete, exit loop

            # Monitor every 1s to avoid high CPU usage
            time.sleep(1)

    except Exception as e:
        logging.error(f"Monitor thread encountered unexpected error: {e}", exc_info=True)
        stop_event.set()
    finally:
        logging.info("Monitor thread terminated.")


def play_song(stop_event, playback_started_event):
    """
    Core playback function with performance optimisations.
    """
    reset_callback_data()

    def check_exit_status():
        if stop_event.is_set():
            logging.warning("Playback failed, exiting.")
            return True
        else:
            return False

    # STAGE 1: Wait for the game to load by detecting the pause button
    logging.info("Waiting for game to load, detecting pause button.")
    template_path = resource_path("assets/resource/image/live/button/pause.png")
    if not template_path.exists():
        logging.error(f"Pause button template image not found: {template_path}")
        return
    template = get_scaled_template(template_path)
    if template is None:
        logging.error(f"Failed to load template image: {template_path}")
        return
    pause_button_found = False
    wait_start_time = time.time()
    playback_started_event.set()
    while not pause_button_found:
        wait_timeout = 30
        wait_current_time = time.time()
        if wait_current_time - wait_start_time > wait_timeout:
            logging.error(f"Waiting for pause button timeout ({wait_current_time - wait_start_time}s), aborting.")
            return
        if check_exit_status():
            return
        screen = current_player.ipc_capture_display()
        height, width, _ = screen.shape
        roi_screen = screen[0:int(height * 0.15), width - int(height * 0.15):width]
        gray_roi = cv2.cvtColor(roi_screen, cv2.COLOR_BGR2GRAY)
        result = cv2.matchTemplate(gray_roi, template, cv2.TM_CCOEFF_NORMED)
        _, max_val, _, _ = cv2.minMaxLoc(result)
        logging.debug(f"Waiting for pause button, match confidence: {max_val:.2f}")
        if max_val >= CONFIDENCE_THRESHOLD_PLAY:
            pause_button_found = True
        else:
            time.sleep(0.5)

    # STAGE 2 & 3: Wait for screen to freeze & photogate detection
    logging.info("Waiting for screen to freeze.")

    def _adjust_offset():
        global callback_data
        total_cost = 0.0
        for type_ in ["up", "down", "move", "wait", "interval"]:
            type_data = callback_data[type_]
            total = type_data["total"]
            if total != 0:
                total_cost += type_data["total_offset"] - OFFSET[type_] * total
                OFFSET[type_] = type_data["total_offset"] / total
        current_chart._a2c_offset += total_cost

    def _get_wait_time():
        wait_for = 0.0
        index = current_chart.actions_to_cmd_index
        for action in current_chart.actions[index - CMD_SLICE_SIZE: index]:
            if action["type"] == "wait":
                wait_for += action["length"]
        return wait_for

    last_color, waited_frames, freezed = None, 0, False
    info = get_runtime_info(current_player.resolution)["wait_first"]
    from_row, to_row = info["from"], info["to"]
    playback_start_time = time.time()
    while True:
        playback_timeout = 500
        playback_current_time = time.time()
        if playback_current_time - playback_start_time > playback_timeout:
            logging.error(f"Playback timeout ({playback_current_time - playback_start_time}s), aborting.")
            return
        if check_exit_status():
            return
        try:
            screen = current_player.ipc_capture_display()
            cur_color, _ = get_color_eval_in_range(screen, from_row, to_row)
            if last_color is not None:
                change_score = np.sum(np.abs(cur_color[:3].astype(int) - last_color[:3].astype(int)))
                if change_score > STABLE_THRESHOLD and freezed:
                    logging.info("First note detected, starting playback.")
                    time.sleep(PHOTOGATE_LATENCY / 1000)
                    break
                elif not freezed:
                    logging.debug(f"Colour change delta: {change_score}, waited_frames: {waited_frames}")
                    if change_score < STABLE_THRESHOLD:
                        waited_frames += 1
                    else:
                        waited_frames = 0
                if not freezed and waited_frames >= CONSECUTIVE_FRAMES_NEEDED:
                    freezed = True
                    logging.info("Screen has frozen. Photogate is ready.")
            last_color = cur_color
            time.sleep(FREEZE_SLEEP_TIME)
        except Exception as e:
            logging.error(f"Error during photogate detection: {e}")
            return

    # STAGE 4: Command execution loop
    logging.info("Starting command execution.")
    while True:
        if check_exit_status():
            return
        current_chart.command_builder.publish(mnt, block=False)
        wait_time = _get_wait_time()
        time.sleep(max(0, wait_time - 3) / 1000)
        index = current_chart.actions_to_cmd_index
        if current_chart.actions[index: index + CMD_SLICE_SIZE]:
            with callback_data_lock:
                _adjust_offset()
                reset_callback_data()
            current_chart.actions_to_MNTcmd((mnt.max_x, mnt.max_y), current_orientation, OFFSET, CMD_SLICE_SIZE)
        else:
            break
    logging.info("Playback finished.")


def mnt_callback(event: MNTEvent, data: MNTEventData):
    global callback_data
    if event == MNTEvent.EVATIVE7_LOG:
        data: MNTEvATive7LogEventData = data
        cmd, cost = data.cmd, data.cost
        cmd_type = cmd.split(" ")[0]
        with callback_data_lock:
            if (last_cmd_endtime := callback_data.get("last_cmd_endtime")) != -1:
                callback_data["interval"]["total"] += 1
                callback_data["interval"]["total_offset"] += (data.start_time - last_cmd_endtime)
            callback_data["last_cmd_endtime"] = data.end_time
            if cmd_type == "w":
                callback_data["wait"]["total"] += 1
                callback_data["wait"]["total_offset"] += cost - int(cmd.split(" ")[-1])
            elif cmd_type in "udm":
                type_ = {"u": "up", "d": "down", "m": "move"}[cmd_type]
                callback_data[type_]["uncommited"] += 1
                callback_data[type_]["total"] += 1
                callback_data[type_]["total_offset"] += cost
            elif cmd_type == "c":
                total_uncommited = sum(callback_data[t]["uncommited"] for t in ["up", "down", "move"])
                if total_uncommited != 0:
                    for t in ["up", "down", "move"]:
                        callback_data[t]["total_offset"] += cost * (callback_data[t]["uncommited"] / total_uncommited)
                        callback_data[t]["uncommited"] = 0


def init_maa():
    global device, maacontroller
    maaresource.post_bundle("assets/resource").wait()
    Toolkit.init_option("./")
    adb_devices = Toolkit.find_adb_devices()
    if not adb_devices: raise RuntimeError("No ADB devices found.")
    supported_devices = [d for d in adb_devices if
                         "mumu" in d.config.get("extras", {}) or "ld" in d.config.get("extras", {})]
    if not supported_devices: raise RuntimeError("No supported emulators found (MuMu, LDPlayer).")
    device = supported_devices[0]
    logging.info(f"Using device: {device.name} at {device.address}")
    maacontroller = AdbController(adb_path=device.adb_path, address=device.address, config=device.config)
    if not maacontroller.post_connection().wait().succeeded:
        raise RuntimeError(f"Failed to connect controller to device {device.name}.")
    maatasker.bind(maaresource, maacontroller)
    if not maatasker.inited: raise RuntimeError("Failed to initialise MAA tasker module.")
    logging.info("MAA initialised successfully.")


def init_player_and_mnt():
    global current_player, mnt
    if not device: raise RuntimeError("MAA device not initialised before initialising player.")
    extra_config = device.config["extras"]
    if "mumu" in extra_config:
        type_, config_key = "mumu", "mumu"
        if "v4" in device.name or "v5" in device.name: type_ += device.name[-2:]
    elif "ld" in extra_config:
        type_, config_key = "ld", "ld"
    else:
        raise RuntimeError(f"Unsupported emulator type: {list(extra_config.keys())}")
    player_config = extra_config[config_key]
    current_player = player.Player(type_, Path(player_config["path"]), player_config["index"])
    mnt = MNT(
        device.address, type_="EvATive7", communicate_type=MNTServerCommunicateType.STDIO,
        mnt_asset_path=resource_path("assets/minitouch_EvATive7"), callback=mnt_callback,
        adb_executor=str(device.adb_path.absolute()),
    )
    logging.info(f"{type_} player and Minitouch initialised successfully.")


# --- MAA Custom Modules ---

@maaresource.custom_recognition("UISongRecognitionFreeSingle")
class UISongRecognitionFreeSingle(CustomRecognition):
    def analyze(self, context: Context, argv: CustomRecognition.AnalyzeArg):
        # This ROI might need adjustment based on the actual screen layout.
        roi = [220, 545, 570, 30]

        def ocr_and_match(model=None):
            try:
                pipeline = {"_ocr_song": {"recognition": "OCR", "roi": roi, "only_rec": True}}
                if model: pipeline["_ocr_song"]["model"] = model
                ocr_text = context.run_recognition("_ocr_song", argv.image, pipeline).best_result.text
                logging.info(f"OCR ({model or 'default'}) raw text: '{ocr_text}'")
                match = fuzzy_match_song(ocr_text)
                logging.info(f"Fuzzy match result ({model or 'default'}): {match}")
                return match
            except Exception as e:
                logging.error(f"OCR ({model or 'default'}) execution failed: {e}")
                return None

        results = [m for m in [ocr_and_match("ppocr_v3/ja_jp"), ocr_and_match()] if m]
        if not results: return self.AnalyzeResult(None, "")
        best_match = max(results, key=lambda x: x[1])
        if best_match and best_match[1] > 50:
            if IS_FULL_SONG:
                song_name = "[FULL] " + best_match[0]
            elif IS_HIGH_DIFFICULTY:
                song_name = "[超高難易度 SPECIAL] " + best_match[0]
            else:
                song_name = best_match[0]
            logging.info(f"Song recognised: '{song_name}' (Confidence: {best_match[1]}%)")
            return self.AnalyzeResult(roi, song_name)
        return self.AnalyzeResult(None, "")


@maaresource.custom_action("UISaveSong")
class UISaveSong(CustomAction):
    def run(self, context, argv):
        save_song(argv.reco_detail.best_result.detail)
        return self.RunResult(True)


@maaresource.custom_action("UIPlay")
class UIPlay(CustomAction):
    def run(self, context, argv):
        global stop_event, playback_started_event
        stop_event.clear()
        playback_started_event.clear()
        monitor = threading.Thread(
            target=monitor_failure_thread,
            args=(stop_event, playback_started_event),
            daemon=True
        )
        try:
            monitor.start()
            play_song(stop_event, playback_started_event)
            stop_event.set()
            return self.RunResult(True)
        except Exception as e:
            stop_event.set()
            logging.error(f"Error during song playback: {e}", exc_info=True)
            return self.RunResult(False)
        finally:
            monitor.join(timeout=5)


# --- Task Entrypoints ---
def init():
    global IS_INITIALISED
    if IS_INITIALISED:
        logging.info("Components are already initialised. Skipping.")
        return
    try:
        init_maa()
        init_player_and_mnt()
        IS_INITIALISED = True
    except Exception as e:
        IS_INITIALISED = False
        logging.error("Initialisation failed.", exc_info=True)
        raise e


# 文件末尾的清理函数
def shutdown_resources():
    """关闭并释放所有全局资源，如 MNT 和 MAA 控制器。"""
    global mnt, maacontroller, maatasker

    if maatasker and maatasker.running:
        logging.info("Final shutdown: Stopping MAA tasker.")
        maatasker.post_stop()

    if mnt:
        logging.info("Disconnecting Minitouch...")
        try:
            mnt.disconnect()
            mnt = None
        except Exception as e:
            logging.error(f"Error disconnecting Minitouch: {e}", exc_info=True)

    if maacontroller:
        logging.info("Disconnecting MAA AdbController...")
        try:
            if maacontroller.post_disconnect().wait().succeeded:
                logging.info("AdbController disconnected successfully.")
            else:
                logging.warning("Failed to disconnect AdbController cleanly.")
            maacontroller = None
        except Exception as e:
            logging.error(f"Error disconnecting AdbController: {e}", exc_info=True)


def run_single_mode_free(config_data):
    """Single song mode: Plays one song and then stops."""
    global DIFFICULTY, HUMAN_DELAY_ENABLED, MANUAL_SONG_NAME, MANUAL_CHART_FILE
    
    DIFFICULTY = config_data.get("difficulty", "expert")
    HUMAN_DELAY_ENABLED = config_data.get("human_delay", False)
    MANUAL_SONG_NAME = config_data.get("manual_song_name", None)
    MANUAL_CHART_FILE = config_data.get("manual_chart_file", None)
    
    if not maacontroller or not mnt:
        raise RuntimeError("MAA is not initialised.")
    
    if MANUAL_SONG_NAME:
        if not load_song_by_name(MANUAL_SONG_NAME):
            logging.error("指定歌曲加载失败，任务终止")
            return
        logging.info("指定歌曲模式：跳过歌曲识别，直接开始")
        override_pipeline = {
            "startlive": {
                "action": "Click",
                "next": ["playsong"],
                "on_error": ["stop"],
                "recognition": "TemplateMatch",
                "template": "live/button/live_medley.png",
                "threshold": 0.5
            },
            "playsong": {
                "action": "Custom",
                "custom_action": "UIPlay",
                "next": ["stop"],
                "timeout": 500000
            },
        }
        maatasker.post_task("startlive", override_pipeline).wait()
        
    elif MANUAL_CHART_FILE:
        logging.info(f"MANUAL_CHART_FILE = {MANUAL_CHART_FILE}")
        logging.info(f"type = {type(MANUAL_CHART_FILE)}")
        if not load_chart_from_file(MANUAL_CHART_FILE):
            logging.error("谱面文件加载失败，任务终止")
            return
        logging.info("指定文件模式：跳过歌曲识别，直接开始")
        override_pipeline = {
            "startlive": {
                "action": "Click",
                "next": ["playsong"],
                "on_error": ["stop"],
                "recognition": "TemplateMatch",
                "template": "live/button/live_medley.png",
                "threshold": 0.5
            },
            "playsong": {
                "action": "Custom",
                "custom_action": "UIPlay",
                "next": ["stop"],
                "timeout": 500000
            },
        }
        maatasker.post_task("startlive", override_pipeline).wait()
        
    else:
        override_pipeline = {
            "ui_simplified_entry": {
                "recognition": "Custom",
                "custom_recognition": "UISongRecognitionFreeSingle",
                "action": "Custom",
                "custom_action": "UISaveSong",
                "next": ["startlive"],
                "timeout": 15000,
                "on_error": ["stop"]
            },
            "startlive": {
                "action": "Click",
                "next": ["playsong"],
                "on_error": ["stop"],
                "recognition": "TemplateMatch",
                "template": "live/button/live_medley.png",
                "threshold": 0.5
            },
            "playsong": {
                "action": "Custom",
                "custom_action": "UIPlay",
                "next": ["stop"],
                "timeout": 500000
            },
        }
        logging.info("Submitting Single Song Mode auto-play task.")
        maatasker.post_task("ui_simplified_entry", override_pipeline).wait()
    
    logging.info("Single Song Mode auto-play task finished.")
