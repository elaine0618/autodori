import logging
import os
import queue
import subprocess
import sys
import threading
import time
import tkinter as tk
from functools import partial
from tkinter import messagebox
from tkinter import scrolledtext
from tkinter import ttk
from tkinter import filedialog
import autodori_ui


class QueueHandler(logging.Handler):
    def __init__(self, log_queue):
        super().__init__()
        self.log_queue = log_queue
    def emit(self, record):
        msg = record.getMessage()
        if msg.startswith("send operation:"):
            return
        self.log_queue.put(self.format(record))

class FilteringFileHandler(logging.FileHandler):
    def emit(self, record):
        msg = record.getMessage()
        if msg.startswith("send operation:"):
            return
        super().emit(record)

class AutodoriGUI:
    def __init__(self, master):
        self.master = master
        master.title("autodori UI")
        master.geometry("700x600")

        self.bot_thread = None

        self.mode_var = tk.StringVar(value='single')
        self.difficulty_var = tk.StringVar(value=autodori_ui.DIFFICULTY)
        self.human_var = tk.BooleanVar(value=autodori_ui.HUMAN_DELAY_ENABLED)
        
        self.input_mode_var = tk.StringVar(value='ocr')

        self.log_queue = queue.Queue()
        queue_handler = QueueHandler(self.log_queue)

        debug_folder = "debug"
        os.makedirs(debug_folder, exist_ok=True)

        log_filename = f"autodori_{time.strftime('%Y%m%d-%H%M%S')}.log"
        log_filepath = os.path.join(debug_folder, log_filename)

        file_handler = FilteringFileHandler(log_filepath, encoding='utf-8')

        root_logger = logging.getLogger()
        root_logger.setLevel(logging.INFO)

        formatter = logging.Formatter('%(asctime)s - [%(levelname)s] - %(message)s', datefmt='%Y-%m-%d %H:%M:%S')

        queue_handler.setFormatter(formatter)
        file_handler.setFormatter(formatter)

        root_logger.addHandler(queue_handler)
        root_logger.addHandler(file_handler)

        self.master.after(100, self._process_log_queue)

        self._create_disclaimer_view()

        self.global_vars_entries = {}

    def _create_disclaimer_view(self):
        """创建并显示欢迎/风险提示界面。"""
        self.master.minsize(700, 550)

        self.disclaimer_frame = ttk.Frame(self.master, padding="15")
        self.disclaimer_frame.pack(fill=tk.BOTH, expand=True)

        title_label = ttk.Label(self.disclaimer_frame, text="使用须知", font=("", 16, "bold"))
        title_label.pack(pady=(10, 20))

        info_text = (
            "在开始前，请仔细阅读以下使用说明与风险提示："
        )
        self.info_label = ttk.Label(self.disclaimer_frame, text=info_text, justify=tk.LEFT, font=("", 11))
        self.info_label.pack(fill=tk.X, pady=5)

        usage_frame = ttk.LabelFrame(self.disclaimer_frame, text="使用方法", padding="10")
        usage_frame.pack(fill=tk.X, pady=10)
        usage_text = (
            "模拟器分辨率请设置为1280x720。\n"
            "歌曲难度请手动设置为与游戏中一致。\n"
            "启动任务前，请手动进入准备打歌界面。\n\n"
            "游戏常规8速，可配合光电微调\n"
            "• 单曲模式：自动识别当前歌曲\n"
            "• 歌名模式：适用于无法识别情况\n"
            "• 文件模式：适用于特殊歌名情况"
        )
        self.usage_label = ttk.Label(usage_frame, text=usage_text, justify=tk.LEFT)
        self.usage_label.pack(fill=tk.X)

        warning_frame = ttk.LabelFrame(self.disclaimer_frame, text="风险提示", padding="10")
        warning_frame.pack(fill=tk.X, pady=10)
        warning_text = (
            "本程序仅于自由、挑战模式下进行开发与测试，不可用于协力模式。\n"
            "程序运行期间，建议您时刻关注模拟器界面。若出现任何异常情况，请立即手动停止任务。\n"
            "使用本程序可能违反游戏的用户协议，您将自行承担一切潜在风险。开发者对由此产生的任何后果概不负责。"
        )
        self.warning_label = ttk.Label(warning_frame, text=warning_text, justify=tk.LEFT, foreground="red")
        self.warning_label.pack(fill=tk.X)

        self.agree_var = tk.BooleanVar()
        agree_check = ttk.Checkbutton(self.disclaimer_frame, text="我已阅读并理解以上条款，同意自行承担所有风险。",
                                      variable=self.agree_var, command=self._toggle_proceed_button_state)
        agree_check.pack(pady=15)

        self.proceed_button = ttk.Button(self.disclaimer_frame, text="进入控制面板", state=tk.DISABLED,
                                         command=self._show_main_app)
        self.proceed_button.pack(fill=tk.X, padx=100, ipady=5)

        self.disclaimer_frame.bind("<Configure>", self._on_disclaimer_resize)

    def _on_disclaimer_resize(self, event):

        new_wraplength = event.width - 40

        self.info_label.config(wraplength=new_wraplength)

        self.usage_label.config(wraplength=new_wraplength - 20)
        self.warning_label.config(wraplength=new_wraplength - 20)

    def _toggle_proceed_button_state(self):
        if self.agree_var.get():
            self.proceed_button.config(state=tk.NORMAL)
        else:
            self.proceed_button.config(state=tk.DISABLED)

    def _show_main_app(self):
        self.disclaimer_frame.destroy()
        self._create_main_widgets()

    def _create_main_widgets(self):
        """创建主控制面板的所有控件。"""
        self.notebook = ttk.Notebook(self.master)
        self.notebook.pack(fill=tk.BOTH, expand=True, padx=5, pady=5)

        control_panel_frame = ttk.Frame(self.notebook, padding="5")
        self.notebook.add(control_panel_frame, text='控制面板')
        self._create_control_panel_tab(control_panel_frame)

        globals_frame = ttk.Frame(self.notebook, padding="10")
        self.notebook.add(globals_frame, text='全局变量调试')
        self._create_globals_tab(globals_frame)

    def _create_control_panel_tab(self, parent_frame):
        """填充"控制面板"选项卡的内容"""
        self.paned_window = ttk.PanedWindow(parent_frame, orient=tk.VERTICAL)
        self.paned_window.pack(fill=tk.BOTH, expand=True)

        top_frame = ttk.Frame(self.paned_window, padding="5")
        self.paned_window.add(top_frame, weight=0)

        style = ttk.Style()
        style.configure("Warning.TLabel", foreground="red")

        config_frame = ttk.LabelFrame(top_frame, text="配置")
        config_frame.pack(fill=tk.X, expand=True, side=tk.TOP, pady=(0, 5))

        mode_frame = ttk.Frame(config_frame)
        mode_frame.pack(fill=tk.X, padx=2, pady=2)
        
        ttk.Label(mode_frame, text="模式:").pack(side=tk.LEFT)

        ttk.Radiobutton(mode_frame, text="OCR识别", variable=self.input_mode_var, value='ocr').pack(side=tk.LEFT, padx=5)
        ttk.Radiobutton(mode_frame, text="指定歌名", variable=self.input_mode_var, value='song_name').pack(side=tk.LEFT, padx=5)
        ttk.Radiobutton(mode_frame, text="本地文件", variable=self.input_mode_var, value='file').pack(side=tk.LEFT, padx=5)

        song_name_row = ttk.Frame(config_frame)
        song_name_row.pack(fill=tk.X, padx=2, pady=2)
        ttk.Label(song_name_row, text="歌名:").pack(side=tk.LEFT)
        self.song_name_entry = ttk.Entry(song_name_row, width=50)
        self.song_name_entry.pack(side=tk.LEFT, padx=5)

        file_row = ttk.Frame(config_frame)
        file_row.pack(fill=tk.X, padx=2, pady=2)
        ttk.Label(file_row, text="谱面文件:").pack(side=tk.LEFT)
        self.file_path_var = tk.StringVar()
        self.file_entry = ttk.Entry(file_row, textvariable=self.file_path_var, width=50)
        self.file_entry.pack(side=tk.LEFT, padx=5)
        ttk.Button(file_row, text="浏览", command=self._select_chart_file).pack(side=tk.LEFT)

        self.input_mode_var.trace_add('write', self._on_input_mode_change)
        self._on_input_mode_change()

        options_frame = ttk.Frame(config_frame)
        options_frame.pack(fill=tk.X, padx=2, pady=2)

        ttk.Label(options_frame, text="难度:").pack(side=tk.LEFT)
        self.difficulty_combobox = ttk.Combobox(options_frame, textvariable=self.difficulty_var,
                                                values=['easy', 'normal', 'hard', 'expert', 'special'], width=10)
        self.difficulty_combobox.pack(side=tk.LEFT, padx=3)

        ttk.Checkbutton(options_frame, text="随机化按键", variable=self.human_var).pack(side=tk.LEFT, padx=10)

        control_frame = ttk.Frame(top_frame)
        control_frame.pack(fill=tk.X, expand=True, side=tk.TOP, pady=(5, 0))
        self.start_button = ttk.Button(control_frame, text="启动任务", command=self.start_bot)
        self.start_button.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=2)
        self.stop_button = ttk.Button(control_frame, text="停止任务", command=self.stop_bot, state=tk.DISABLED)
        self.stop_button.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=2)

        log_frame = ttk.Frame(self.paned_window, padding="0")
        self.paned_window.add(log_frame, weight=1)
        self.log_display = scrolledtext.ScrolledText(log_frame, state='disabled', wrap=tk.WORD, bg="#2b2b2b", fg="white")
        self.log_display.pack(fill=tk.BOTH, expand=True)

        self.paned_window.bind("<ButtonPress-1>", self._prevent_resize)
        self.paned_window.bind("<B1-Motion>", self._prevent_resize)
    def _select_chart_file(self):
        """选择谱面JSON文件"""
        filename = filedialog.askopenfilename(
            title="选择谱面文件",
            filetypes=[("JSON files", "*.json"), ("All files", "*.*")]
        )
        if filename:
            self.file_path_var.set(filename)

    def _on_input_mode_change(self, *args):
        """输入模式切换时禁用/启用对应的输入框"""
        mode = self.input_mode_var.get()
        
        if mode == 'ocr':
            self.song_name_entry.config(state='disabled')
            self.file_entry.config(state='disabled')
            self.file_path_var.set("")
        elif mode == 'song_name':
            self.song_name_entry.config(state='normal')
            self.file_entry.config(state='disabled')
            self.file_path_var.set("")
        elif mode == 'file':
            self.song_name_entry.config(state='disabled')
            self.song_name_entry.delete(0, tk.END)
            self.file_entry.config(state='normal')

    def _create_globals_tab(self, parent_frame):
        """填充“全局变量调试”选项卡的内容"""
        self.editable_globals = {
            'PHOTOGATE_LATENCY': int,
            'MIN_LIVEBOOST': int,
            'DEFAULT_MOVE_SLICE_SIZE': int,
            'CMD_SLICE_SIZE': int,
            'MAX_CONTINUOUS_FAILED_TIMES': int,
            'STABLE_THRESHOLD': int,
            'CONSECUTIVE_FRAMES_NEEDED': int,
            'FREEZE_SLEEP_TIME': float,
            'CONFIDENCE_THRESHOLD_FAILURE': float,
            'CONFIDENCE_THRESHOLD_PLAY': float,
            'IS_FULL_SONG': bool,
            'IS_HIGH_DIFFICULTY': bool
        }

        localization_map = {
            'PHOTOGATE_LATENCY': "光电门延迟 (ms)",
            'MIN_LIVEBOOST': "最小 LiveBoost 值",
            'DEFAULT_MOVE_SLICE_SIZE': "滑动音符切片大小",
            'CMD_SLICE_SIZE': "指令分片大小",
            'MAX_CONTINUOUS_FAILED_TIMES': "最大连续失败次数",
            'STABLE_THRESHOLD': "画面静止判定阈值",
            'CONSECUTIVE_FRAMES_NEEDED': "画面静止所需帧数",
            'FREEZE_SLEEP_TIME': "画面静止检测间隔时间 (s)",
            'CONFIDENCE_THRESHOLD_FAILURE': "失败检测置信度",
            'CONFIDENCE_THRESHOLD_PLAY': "歌曲开始检测置信度",
            'IS_FULL_SONG': "FULL乐曲支持",
            'IS_HIGH_DIFFICULTY': "超高难易度支持"
        }

        parent_frame.columnconfigure(1, weight=1)

        current_row = 0
        for var_name, var_type in self.editable_globals.items():

            display_name = localization_map.get(var_name, var_name)
            ttk.Label(parent_frame, text=f"{display_name}:", font=("", 10)).grid(row=current_row, column=0, sticky='w',
                                                                                 padx=5, pady=5)

            if var_type == dict:
                dict_frame = ttk.Frame(parent_frame)
                dict_frame.grid(row=current_row, column=1, sticky='ew', padx=5, pady=2)
                self.global_vars_entries[var_name] = {}

                current_dict = getattr(autodori_ui, var_name)

                col_count = 0
                for key, value in current_dict.items():
                    ttk.Label(dict_frame, text=key).grid(row=0, column=col_count, padx=(0, 2))
                    entry_var = tk.StringVar(value=str(value))
                    entry = ttk.Entry(dict_frame, textvariable=entry_var, width=8)
                    entry.grid(row=0, column=col_count + 1, padx=(0, 10))

                    self.global_vars_entries[var_name][key] = entry_var
                    col_count += 2

            elif var_type == bool:
                bool_var = tk.BooleanVar(value=getattr(autodori_ui, var_name))
                checkbutton = ttk.Checkbutton(parent_frame, variable=bool_var)
                checkbutton.grid(row=current_row, column=1, sticky='w', padx=5, pady=2)
                self.global_vars_entries[var_name] = bool_var

            else:
                entry_var = tk.StringVar(value=str(getattr(autodori_ui, var_name)))
                entry = ttk.Entry(parent_frame, textvariable=entry_var)
                entry.grid(row=current_row, column=1, sticky='ew', padx=5, pady=2)
                self.global_vars_entries[var_name] = entry_var

            current_row += 1

        ttk.Separator(parent_frame, orient='horizontal').grid(row=current_row, column=0, columnspan=2, sticky='ew',
                                                              pady=15)
        current_row += 1

        button_frame = ttk.Frame(parent_frame)
        button_frame.grid(row=current_row, column=0, columnspan=2, sticky='e')

        apply_button = ttk.Button(button_frame, text="应用修改", command=self._apply_global_settings)
        apply_button.pack(side=tk.RIGHT, padx=5)

    def _apply_global_settings(self):
        """将UI中的值应用到后端的全局变量"""
        try:
            for var_name, controls in self.global_vars_entries.items():
                var_type = self.editable_globals[var_name]

                if var_type == dict:
                    current_dict = getattr(autodori_ui, var_name)
                    for key, entry_var in controls.items():
                        new_val_str = entry_var.get()
                        try:
                            current_dict[key] = float(new_val_str)
                        except ValueError:
                            current_dict[key] = int(new_val_str)
                else:  # int or float
                    new_val_str = controls.get()
                    if var_type == int:
                        setattr(autodori_ui, var_name, int(new_val_str))
                    elif var_type == float:
                        setattr(autodori_ui, var_name, float(new_val_str))
                    elif var_type == bool:
                        setattr(autodori_ui, var_name, new_val_str)

            messagebox.showinfo("成功", "全局变量已成功更新！")

        except ValueError as e:
            messagebox.showerror("输入错误", f"修改失败，请输入有效的数值。\n错误: {e}")
        except Exception as e:
            messagebox.showerror("未知错误", f"应用设置时发生错误: {e}")

    def _process_log_queue(self):
        try:
            while True:
                record = self.log_queue.get(block=False)
                if hasattr(self, 'log_display'):
                    self.log_display.configure(state='normal')
                    self.log_display.insert(tk.END, record + '\n')
                    self.log_display.see(tk.END)
                    self.log_display.configure(state='disabled')

        except queue.Empty:
            pass
        self.master.after(100, self._process_log_queue)

    def start_bot(self):
        input_mode = self.input_mode_var.get()
        if input_mode == 'song_name':
            song_name = self.song_name_entry.get().strip()
            if not song_name:
                messagebox.showerror("输入错误", "请输入歌名！")
                return
        elif input_mode == 'file':
            file_path = self.file_path_var.get().strip()
            if not file_path:
                messagebox.showerror("输入错误", "请选择谱面文件！")
                return
            if not os.path.exists(file_path):
                messagebox.showerror("输入错误", f"文件不存在: {file_path}")
                return
        
        self.start_button.config(state=tk.DISABLED)
        self.stop_button.config(state=tk.NORMAL)
        self.notebook.tab(1, state='disabled')

        config_data = {
            "mode": 'single',
            "difficulty": self.difficulty_var.get(),
            "human_delay": self.human_var.get(),
            "input_mode": input_mode,
            "manual_song_name": self.song_name_entry.get().strip() if input_mode == 'song_name' else None,
            "manual_chart_file": self.file_path_var.get().strip() if input_mode == 'file' else None,
        }
        self.bot_thread = threading.Thread(target=self._run_bot_task, args=(config_data,), daemon=True)
        self.bot_thread.start()

    def stop_bot(self):
        if hasattr(autodori_ui, 'stop_event'):
            logging.info("Sending stop signal to internal loop.")
            autodori_ui.stop_event.set()

        if hasattr(autodori_ui, 'maatasker') and autodori_ui.maatasker and autodori_ui.maatasker.running:
            logging.info("Attempting to stop maatasker.")
            autodori_ui.maatasker.post_stop()
        else:
            pass

    def _run_bot_task(self, config_data):
        try:
            logging.info("Initialising.")
            autodori_ui.init()
            logging.info("Initialisation complete.")

            mode = config_data.get("mode")
            if mode == 'single':
                autodori_ui.run_single_mode_free(config_data)
            else:
                logging.error(f"未知模式: {mode}")

            logging.info("Task completed.")
        except Exception as e:
            logging.error(f"Exception: {e}", exc_info=True)
        finally:
            self.start_button.config(state=tk.NORMAL)
            self.stop_button.config(state=tk.DISABLED)
            self.notebook.tab(1, state='normal')

    def _prevent_resize(self, event):
        return "break"

    def on_app_exit(self):
        logging.info("Application exit requested. Cleaning up resources...")

        if self.bot_thread and self.bot_thread.is_alive():
            logging.info("Active task found. Calling stop_bot() before exiting.")
            self.stop_bot()
            self.bot_thread.join(timeout=5)

        logging.info("Shutting down backend resources (Minitouch, ADB)...")
        autodori_ui.shutdown_resources()

        logging.info("Cleanup complete. Exiting GUI.")
        self.master.destroy()


if __name__ == "__main__":
    if sys.platform == 'win32':
        startupinfo = subprocess.STARTUPINFO()
        startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        subprocess.Popen = partial(subprocess.Popen, startupinfo=startupinfo)
    root = tk.Tk()
    app = AutodoriGUI(root)
    root.mainloop()