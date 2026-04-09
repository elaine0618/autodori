import json
import logging
import statistics
import time
from pathlib import Path

import numpy as np
from minitouchpy import CommandBuilder

def get_runtime_info(resolution: tuple[int, int]):
    x_zoom_multiple = resolution[0] / 1280
    y_zoom_multiple = resolution[1] / 720

    def get_rounded_int_x(origin):
        return int(round(origin * x_zoom_multiple, 0))

    def get_rounded_int_y(origin):
        return int(round(origin * y_zoom_multiple, 0))

    return {
        "lane": {
            "w": get_rounded_int_x(147),
            "start_x": get_rounded_int_x(127),
            "h": get_rounded_int_y(590),
        },
        "wait_first": {
            "from": get_rounded_int_y(510),
            "to": get_rounded_int_y(535),
        },
    }

def get_color_eval_in_range(image_array, start_row, end_row):
    avg_color = np.zeros(3)
    std_color = np.zeros(3)

    for row_index in range(start_row, end_row + 1):
        avg_color_row, std_color_row = evaluate_row_color(image_array, row_index)
        avg_color += np.array(avg_color_row)
        std_color += np.array(std_color_row)

    avg_color /= end_row - start_row + 1
    std_color /= end_row - start_row + 1

    return avg_color, std_color


def evaluate_row_color(image_array, row_index):
    """
    评估图像中某一行的颜色（仅RGB）。
    :param image_array: 输入图像，应为 (height, width, 3) 的 numpy 数组
    :param row_index: 要评估的行索引
    :return: 返回该行的平均颜色 (R, G, B) 和标准差
    """
    row_data = image_array[row_index, :, :]  # 形状为 (width, 3)

    # 分离出 R、G、B 通道
    r, g, b = row_data[:, 0], row_data[:, 1], row_data[:, 2]

    avg_color = (np.mean(r), np.mean(g), np.mean(b))
    std_color = (np.std(r), np.std(g), np.std(b))

    return avg_color, std_color


def resolution_to_xformat(resolution: tuple[int, int]):
    resolution_x, resolution_y = resolution
    return f"{resolution_x}x{resolution_y}"


def androidxy_to_MNTxy(android, mnt_resolution: tuple[int, int], orientation: int):
    android_x, android_y = android
    resolution_x, resolution_y = mnt_resolution

    list_ = [-1] * 4
    list_[orientation] = android_x
    list_[orientation + 1] = android_y
    for i in range(len(list_)):
        if list_[i] == -1:
            if i == 0:
                list_[0] = resolution_x - list_[2]
            elif i == 1:
                list_[1] = resolution_y - list_[3]
            elif i == 2:
                list_[2] = resolution_x - list_[0]
            elif i == 3:
                list_[3] = resolution_y - list_[1]

    return (int(list_[0]), int(list_[1]))
