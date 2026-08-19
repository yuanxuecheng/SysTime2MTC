#!/usr/bin/env python3
"""
LouDuck MTC Sync - macOS 菜单栏应用
用系统时间生成 MIDI Timecode，通过虚拟 MIDI 端口同步 Pro Tools
"""

import sys
import time
import threading
import signal
from datetime import datetime

import rtmidi
from PySide6.QtCore import QTimer, Qt, QObject, Signal
from PySide6.QtGui import QAction, QFont, QIcon
from PySide6.QtWidgets import (
    QApplication, QSystemTrayIcon, QMenu, QLabel, QWidgetAction,
    QDialog, QVBoxLayout, QHBoxLayout, QPushButton, QComboBox,
    QSpinBox, QGroupBox, QFormLayout, QMessageBox
)


# ============== MTC 生成器核心 ==============

class MtcGenerator(QObject):
    """MTC 生成器 - 用高精度线程发送 Quarter Frame 消息"""
    
    timecode_changed = Signal(str)  # 时码更新信号
    status_changed = Signal(str)    # 状态更新信号
    
    # MTC 速率码
    RATE_24FPS = 0x00
    RATE_25FPS = 0x01
    RATE_30DF = 0x02  # 29.97 drop frame
    RATE_30FPS = 0x03
    
    RATE_MAP = {
        24: RATE_24FPS,
        25: RATE_25FPS,
        29: RATE_30DF,
        30: RATE_30FPS,
    }
    
    def __init__(self):
        super().__init__()
        self.midi_out = rtmidi.MidiOut()
        self.running = False
        self.thread = None
        self.fps = 25
        self.port_name = "LouDuck MTC Sync"
        self._port_opened = False
        
    def get_available_ports(self):
        """获取可用 MIDI 输出端口列表"""
        return self.midi_out.get_ports()
    
    def open_port(self, port_index=None):
        """打开 MIDI 端口"""
        try:
            if port_index is not None:
                self.midi_out.open_port(port_index)
            else:
                self.midi_out.open_virtual_port(self.port_name)
            self._port_opened = True
            return True
        except Exception as e:
            print(f"打开 MIDI 端口失败: {e}")
            return False
    
    def close_port(self):
        """关闭 MIDI 端口"""
        self.stop()
        if self._port_opened:
            self.midi_out.close_port()
            self._port_opened = False
    
    def _time_to_mtc_data(self, hours, minutes, seconds, frames):
        """将时间转换为 8 个 Quarter Frame 数据"""
        rate_nibble = self.RATE_MAP.get(self.fps, self.RATE_25FPS) << 1
        return [
            (0 << 4) | (frames & 0x0F),           # 帧低位
            (1 << 4) | ((frames >> 4) & 0x01),    # 帧高位
            (2 << 4) | (seconds & 0x0F),          # 秒低位
            (3 << 4) | ((seconds >> 4) & 0x03),   # 秒高位
            (4 << 4) | (minutes & 0x0F),          # 分低位
            (5 << 4) | ((minutes >> 4) & 0x03),   # 分高位
            (6 << 4) | (hours & 0x0F),            # 时低位
            (7 << 4) | ((hours >> 4) & 0x01) | rate_nibble,  # 时高位 + 速率
        ]
    
    def _send_full_frame(self, hours, minutes, seconds, frames):
        """发送 MTC Full Frame (用于定位)"""
        rate_byte = self.RATE_MAP.get(self.fps, self.RATE_25FPS) << 5
        msg = [
            0xF0, 0x7F, 0x7F, 0x01,
            rate_byte,
            hours & 0x1F,
            minutes & 0x3F,
            seconds & 0x3F,
            frames & 0x1F,
            0xF7
        ]
        self.midi_out.send_message(msg)
    
    def _send_quarter_frame(self, piece, value):
        """发送单个 Quarter Frame"""
        msg = [0xF1, (piece << 4) | (value & 0x0F)]
        self.midi_out.send_message(msg)
    
    def _get_system_timecode(self):
        """获取当前系统时间作为时码"""
        now = datetime.now()
        # 用当天经过的毫秒数计算帧
        total_ms = (now.hour * 3600 + now.minute * 60 + now.second) * 1000 + now.microsecond // 1000
        frame_duration_ms = 1000.0 / self.fps
        frames = int((total_ms % 1000) / frame_duration_ms)
        return now.hour, now.minute, now.second, frames
    
    def _generator_loop(self):
        """高精度 MTC 生成循环"""
        qf_interval = 1.0 / (self.fps * 8)  # Quarter Frame 间隔
        next_time = time.perf_counter()
        
        while self.running:
            h, m, s, f = self._get_system_timecode()
            mtc_data = self._time_to_mtc_data(h, m, s, f)
            
            # 每帧发送 8 个 Quarter Frame
            for piece in range(8):
                if not self.running:
                    break
                self._send_quarter_frame(piece, mtc_data[piece])
                
                # 精确等待到下一个 QF 时间点
                next_time += qf_interval
                sleep_time = next_time - time.perf_counter()
                if sleep_time > 0:
                    time.sleep(sleep_time)
                else:
                    # 如果落后了，重置计时器
                    next_time = time.perf_counter() + qf_interval
            
            # 每帧更新一次显示
            tc_str = f"{h:02d}:{m:02d}:{s:02d}:{f:02d}"
            self.timecode_changed.emit(tc_str)
    
    def start(self):
        """开始发送 MTC"""
        if not self._port_opened:
            self.status_changed.emit("错误: MIDI 端口未打开")
            return False
        
        if self.running:
            return True
        
        self.running = True
        self.thread = threading.Thread(target=self._generator_loop, daemon=True)
        self.thread.start()
        
        # 先发送 Full Frame 定位
        h, m, s, f = self._get_system_timecode()
        self._send_full_frame(h, m, s, f)
        
        self.status_changed.emit("运行中")
        return True
    
    def stop(self):
        """停止发送 MTC"""
        self.running = False
        if self.thread:
            self.thread.join(timeout=0.5)
            self.thread = None
        self.status_changed.emit("已停止")


# ============== 设置对话框 ==============

class SettingsDialog(QDialog):
    """设置对话框"""
    
    def __init__(self, mtc_gen, parent=None):
        super().__init__(parent)
        self.mtc_gen = mtc_gen
        self.setWindowTitle("SysTime2MTC 设置")
        self.setFixedSize(400, 300)
        
        layout = QVBoxLayout()
        
        # FPS 设置
        fps_group = QGroupBox("帧率")
        fps_layout = QFormLayout()
        self.fps_combo = QComboBox()
        self.fps_combo.addItems(["24 fps", "25 fps", "29.97 df", "30 fps"])
        self.fps_combo.setCurrentIndex(1)  # 默认 25fps
        fps_layout.addRow("时码帧率:", self.fps_combo)
        fps_group.setLayout(fps_layout)
        layout.addWidget(fps_group)
        
        # MIDI 端口设置
        port_group = QGroupBox("MIDI 端口")
        port_layout = QFormLayout()
        self.port_combo = QComboBox()
        self._refresh_ports()
        port_layout.addRow("输出端口:", self.port_combo)
        
        refresh_btn = QPushButton("刷新端口列表")
        refresh_btn.clicked.connect(self._refresh_ports)
        port_layout.addRow(refresh_btn)
        
        port_group.setLayout(port_layout)
        layout.addWidget(port_group)
        
        # 按钮
        btn_layout = QHBoxLayout()
        ok_btn = QPushButton("应用")
        ok_btn.clicked.connect(self._apply_settings)
        cancel_btn = QPushButton("取消")
        cancel_btn.clicked.connect(self.reject)
        btn_layout.addStretch()
        btn_layout.addWidget(ok_btn)
        btn_layout.addWidget(cancel_btn)
        layout.addLayout(btn_layout)
        
        layout.addStretch()
        self.setLayout(layout)
    
    def _refresh_ports(self):
        """刷新 MIDI 端口列表"""
        self.port_combo.clear()
        ports = self.mtc_gen.get_available_ports()
        if not ports:
            self.port_combo.addItem("无可用端口 (将创建虚拟端口)")
        else:
            for i, name in enumerate(ports):
                self.port_combo.addItem(f"{i}: {name}", i)
            self.port_combo.addItem("创建虚拟端口", None)
    
    def _apply_settings(self):
        """应用设置"""
        # 设置 FPS
        fps_map = {0: 24, 1: 25, 2: 29, 3: 30}
        self.mtc_gen.fps = fps_map.get(self.fps_combo.currentIndex(), 25)
        
        # 重新打开端口
        self.mtc_gen.close_port()
        
        port_data = self.port_combo.currentData()
        if port_data is None:
            success = self.mtc_gen.open_port()  # 虚拟端口
        else:
            success = self.mtc_gen.open_port(port_data)
        
        if success:
            QMessageBox.information(self, "成功", "设置已应用")
            self.accept()
        else:
            QMessageBox.warning(self, "错误", "无法打开 MIDI 端口")


# ============== 主应用 ==============

class MtcSyncApp(QApplication):
    """主应用程序"""
    
    def __init__(self):
        super().__init__(sys.argv)
        self.setQuitOnLastWindowClosed(False)
        
        # MTC 生成器
        self.mtc = MtcGenerator()
        
        # 创建菜单栏图标
        self.tray = QSystemTrayIcon()
        # 使用系统图标或自定义图标
        self.tray.setIcon(self.style().standardIcon(self.style().SP_MediaPlay))
        self.tray.setToolTip("SysTime2MTC - 已停止")
        
        # 创建菜单
        self.menu = QMenu()
        
        # 时码显示
        self.tc_label = QLabel("00:00:00:00")
        self.tc_label.setFont(QFont("Menlo", 16, QFont.Bold))
        self.tc_label.setStyleSheet("color: #00ff00; padding: 8px 16px;")
        self.tc_action = QWidgetAction(self.menu)
        self.tc_action.setDefaultWidget(self.tc_label)
        self.menu.addAction(self.tc_action)
        
        self.menu.addSeparator()
        
        # 状态显示
        self.status_label = QLabel("状态: 已停止")
        self.status_label.setStyleSheet("color: #888; padding: 4px 16px;")
        status_action = QWidgetAction(self.menu)
        status_action.setDefaultWidget(self.status_label)
        self.menu.addAction(status_action)
        
        self.menu.addSeparator()
        
        # 控制按钮
        self.start_action = QAction("▶ 开始同步", self)
        self.start_action.triggered.connect(self._start_sync)
        self.menu.addAction(self.start_action)
        
        self.stop_action = QAction("⏹ 停止同步", self)
        self.stop_action.triggered.connect(self._stop_sync)
        self.stop_action.setEnabled(False)
        self.menu.addAction(self.stop_action)
        
        self.menu.addSeparator()
        
        # 设置
        settings_action = QAction("⚙ 设置...", self)
        settings_action.triggered.connect(self._show_settings)
        self.menu.addAction(settings_action)
        
        self.menu.addSeparator()
        
        # 关于
        about_action = QAction("关于", self)
        about_action.triggered.connect(self._show_about)
        self.menu.addAction(about_action)
        
        # 退出
        quit_action = QAction("退出", self)
        quit_action.triggered.connect(self._quit)
        self.menu.addAction(quit_action)
        
        self.tray.setContextMenu(self.menu)
        self.tray.activated.connect(self._tray_activated)
        self.tray.show()
        
        # 连接信号
        self.mtc.timecode_changed.connect(self._update_timecode)
        self.mtc.status_changed.connect(self._update_status)
        
        # 初始化 MIDI 端口
        self._init_midi()
        
        # 处理信号
        signal.signal(signal.SIGINT, self._signal_handler)
    
    def _init_midi(self):
        """初始化 MIDI 端口"""
        ports = self.mtc.get_available_ports()
        if ports:
            # 尝试打开第一个端口
            self.mtc.open_port(0)
        else:
            # 创建虚拟端口
            self.mtc.open_port()
    
    def _start_sync(self):
        """开始同步"""
        if self.mtc.start():
            self.start_action.setEnabled(False)
            self.stop_action.setEnabled(True)
            self.tray.setIcon(self.style().standardIcon(self.style().SP_MediaStop))
    
    def _stop_sync(self):
        """停止同步"""
        self.mtc.stop()
        self.start_action.setEnabled(True)
        self.stop_action.setEnabled(False)
        self.tray.setIcon(self.style().standardIcon(self.style().SP_MediaPlay))
        self.tc_label.setText("00:00:00:00")
    
    def _update_timecode(self, tc_str):
        """更新时码显示"""
        self.tc_label.setText(tc_str)
    
    def _update_status(self, status):
        """更新状态显示"""
        self.status_label.setText(f"状态: {status}")
        self.tray.setToolTip(f"SysTime2MTC - {status}")
    
    def _show_settings(self):
        """显示设置对话框"""
        dialog = SettingsDialog(self.mtc)
        dialog.exec()
    
    def _show_about(self):
        """显示关于信息"""
        QMessageBox.about(
            None,
            "关于 SysTime2MTC",
            "<h2>SysTime2MTC</h2>"
            "<p>用 macOS 系统时间生成 MTC 时码</p>"
            "<p>同步 Pro Tools 走带</p>"
            "<p>版本: 1.0.0</p>"
        )
    
    def _tray_activated(self, reason):
        """点击托盘图标"""
        if reason == QSystemTrayIcon.ActivationReason.Trigger:
            self.menu.popup(self.tray.geometry().center())
    
    def _signal_handler(self, signum, frame):
        """处理系统信号"""
        self._quit()
    
    def _quit(self):
        """退出应用"""
        self.mtc.close_port()
        self.tray.hide()
        self.quit()


# ============== 入口 ==============

if __name__ == "__main__":
    app = MtcSyncApp()
    sys.exit(app.exec())
