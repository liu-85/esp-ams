"""
AMS_MODEL.py —— AMS 业务逻辑（换料调度）
==========================================

本文件负责：
    · 与拓竹打印机通过 MQTT 通信
    · 接收打印机的换料请求（暂停 + M73 P101）
    · 驱动"共享电机 + 4 路电磁离合"完成退料 / 进料
    · 通知打印机继续打印

--------------------------------------------------------------------------
硬件层（本版新增）
--------------------------------------------------------------------------
整机只有 **1 个共享直流电机 + 4 路电磁离合**：

    self.motor_bus      —— FilamentMotorBus，负责电机方向与离合仲裁
    self.meterial_list  —— 4 个 material 对象，分别对应物理料盘位 1~4
    self.dianji_dict    —— 打印机通道号 → material 对象的映射（由 access_list 决定）

★ 硬性约束：任何时刻最多只能有 1 路电磁离合吸合。
  这条约束由 FilamentMotorBus 在软件层强制保证：
      · 每次吸合前先无条件下发"全部断开"
      · 吸合前回读复核，发现残留立即全部断开并取消动作
      · 换料流程用 try/finally 包住，异常时也一定停电机 + 断开全部离合
      · 主循环每个周期调用 assert_single() 体检
  业务代码**不需要**自己做互斥判断，但要避免自己去写离合的 GPIO。

--------------------------------------------------------------------------
换料时序（与老版本保持一致，只是底层换了执行机构）
--------------------------------------------------------------------------
    1. 打印机进入暂停换料状态（M73 P101 + M400 U1）
    2. AMS 发 M83，切到相对挤出模式
    3. 打印机退料：G1 E-50 F200
    4. AMS 侧收料：吸合【旧】通道离合 → 电机反转 → 停 → 断开离合
    5. AMS 侧送料：吸合【新】通道离合 → 电机正转 → 停 → 断开离合
    6. 打印机进料：G1 E50 F200，同时 AMS 辅助推料一小段
    7. 记录当前料盘并持久化到 config.json
    8. 发 resume 让打印机继续打印
"""

from device_processing import material, build_motor_bus, build_materials
from bambu.bambu_mqtt import Bambu_mqtt_cliet
import time
from logout import logout
from bambu.bambu_commands import banbu_start, START_PUSH
import ujson
from bambu.bambu_commands import *
from info_load import read_json_file, write_json_file
import uasyncio as asyncio

from hardware_config import (
    CONFIG_FILE,
    RETRACT_STEPS,
    LOAD_RETRY_TIMES,
    LOAD_ASSIST_MS,
    NO_LIMIT_LOAD_MS,
    NO_LIMIT_RETRACT_MS,
    NO_LIMIT_PROBE_MS,
)
from motor_clutch import ClutchConflictError, MotorBusError


# ==========================================================================
# 主循环节奏（毫秒）
# ==========================================================================
#   AMS_POLL_MS        非阻塞收包的轮询间隔。越小响应越快，200ms 足够，
#                      而且这个 await 会把 CPU 让给 Web / 状态灯任务。
#   PUSH_INTERVAL_MS   多久向打印机查询一次状态。打印机自己也会主动上报，
#                      1 秒一次就够。旧代码隐含节奏也是 ~1 次/秒。
#   RECONNECT_INTERVAL_MS  定期重建 MQTT 连接的间隔。旧代码是"每 20 轮"，
#                      而每轮都阻塞等消息，实际变成几秒断一次，非常不稳；
#                      改成 5 分钟一次。
AMS_POLL_MS = 200
PUSH_INTERVAL_MS = 1000
RECONNECT_INTERVAL_MS = 300000


class AMS(Bambu_mqtt_cliet):
    def __init__(self):
        super().__init__(mqtt_server="", DEVICE_SERIAL="", password="")   # 继承MQTT类

        # ------------------------------------------------------------------
        # 硬件层：1 个共享电机 + 4 路电磁离合
        # ------------------------------------------------------------------
        self.motor_bus = build_motor_bus()
        self.meterial_list = build_materials(self.motor_bus)

        # ------------------------------------------------------------------
        # 业务层：打印机通道号映射
        # ------------------------------------------------------------------
        self.color_list = ["red" for n in range(len(self.meterial_list))]
        self.access_list = [n + 1 for n in range(len(self.meterial_list))]  # 颜色映射通道
        # 打印机通道号 -> 物理料盘位对象
        self.dianji_dict = {key: value for key, value in zip(self.access_list, self.meterial_list)}

        self.filament_current = 0   # 当前正在用的料盘（打印机通道号，0 = 未知）
        self.now_warring = ""
        self.config_file = CONFIG_FILE

    # ======================================================================
    # 日志
    # ======================================================================
    def update_warring(self, text, is_error=False):
        if len(self.now_warring) >= 30:
            indexs = self.now_warring.find("\n")
            self.now_warring = self.now_warring[indexs:]
        self.now_warring += (text + "\n")

    # ======================================================================
    # 配置加载 / 保存
    # ======================================================================
    def auto_update_access(self, file_path=None):
        """从配置文件恢复通道映射、颜色和当前料盘"""
        file_path = file_path or self.config_file
        file_data = read_json_file(file_path)
        if not file_data:
            self.filament_current = 0
            return False

        color_list = file_data.get("color_list", None)
        if color_list:
            self.color_list = color_list

        new_data = file_data.get("access", None)
        if new_data:
            self.access_list = new_data
            self.dianji_dict = {key: value for key, value in zip(self.access_list, self.meterial_list)}

        # 恢复上次记录的当前料盘（没有到位开关时这一步很关键）
        saved = file_data.get("filament_current", 0)
        if saved:
            self.filament_current = saved
            logout("从配置恢复当前料盘: %s" % saved)

        self.filament_current = self.now_filament(self.filament_current)
        return bool(new_data)

    def save_current_filament(self):
        """把当前料盘号写回 config.json，断电重启后能恢复"""
        try:
            data = read_json_file(self.config_file)
            if not data:
                logout("保存当前料盘失败：配置读取为空", is_error=True)
                return False
            data["filament_current"] = self.filament_current
            write_json_file(self.config_file, data)
            return True
        except Exception as e:
            logout("保存当前料盘失败:" + str(e), is_error=True)
            return False

    # ======================================================================
    # 到位开关能力查询
    # ======================================================================
    def has_any_limit(self):
        """整机是否安装了任意一个到位开关"""
        for mat in self.meterial_list:
            if mat.has_limit:
                return True
        return False

    # ======================================================================
    # 探测当前料盘
    # ======================================================================
    def now_filament(self, start_filamet=1):
        """探测当前正在使用的是哪个料盘（返回打印机通道号，0 表示未知）。

        探测原理（需要安装到位开关）：
            当前料盘位里有料，反向转动时料线绷紧，会把浮动轮/送料臂推到限位，
            触发行程开关；空料盘位反转时轮子空转，开关不动。
            探测到之后再把料正转回去，避免长期绷紧。

        降级模式（未安装到位开关）：
            无法探测，直接沿用 config.json 里记录的 filament_current。
            所以这种情况下**千万不要手动插拔料盘**，否则记录会和实际不一致。
        """
        if not self.dianji_dict:
            return 0

        # 探测顺序：先试上次用的通道，再试其余通道
        probe_order = []
        if 0 < start_filamet <= len(self.meterial_list):
            probe_order.append(start_filamet)
        for key in self.dianji_dict:
            if key != start_filamet:
                probe_order.append(key)

        if not self.has_any_limit():
            logout("未安装到位开关，跳过料盘探测，沿用记录值: %s" % self.filament_current)
            return self.filament_current if self.filament_current else 0

        step_time = 500
        max_steps = max(1, NO_LIMIT_PROBE_MS // step_time)

        for key in probe_order:
            mat = self.dianji_dict.get(key, None)
            if mat is None or not mat.has_limit:
                continue
            logout("开始获取当前料盘id" + str(key))
            count = 0
            try:
                # 整段探测期间保持该通道离合吸合，避免反复吸合/断开
                with mat.hold():
                    while count < max_steps:
                        count += 1
                        self.motor_bus.motor.set_direction(-1)
                        time.sleep_ms(step_time)
                        if mat.limit_triggered(-1):
                            # 触发后反向回位，把刚才拉紧的料退回去
                            self.motor_bus.motor.run(1, count * step_time)
                            self.motor_bus.motor.stop()
                            logout("探测到当前料盘: %s" % key)
                            return key
                    self.motor_bus.motor.stop()
            except MotorBusError as e:
                logout("探测通道%s 时总线异常: %s" % (key, e), is_error=True)
                self.motor_bus.release_all()
        return 0

    # ======================================================================
    # 推料动作
    # ======================================================================
    def fileament_move(self, fileament_id, counts=10, orientation=1):
        """把 fileament_id 通道的料推动 counts 步。

        返回值：
            True  —— 动作正常（有到位开关时表示没检测到异常；无开关时表示按时间推进完成）
            False —— 检测到异常（料已到底 / 卡住），调用方应停止继续推送

        有到位开关时：每推一步查一次开关，超过 20% 的采样点触发就认为到底了。
        无到位开关时：只能按时间推进，总时长会被 NO_LIMIT_* 参数封顶，
                      并且恒定返回 True（无法判定异常）。
        """
        if fileament_id not in self.dianji_dict:
            logout("通道 %s 不存在，无法推料" % fileament_id, is_error=True)
            return False

        mat = self.dianji_dict[fileament_id]

        # ---------------- 降级模式：没有到位开关 ----------------
        if not mat.has_limit:
            total_ms = counts * mat.step_ms
            cap = NO_LIMIT_LOAD_MS if orientation == 1 else NO_LIMIT_RETRACT_MS
            if cap and total_ms > cap:
                total_ms = cap
            logout("通道%s 无到位开关，按时间推进 %dms（方向%s）"
                   % (fileament_id, total_ms, "进料" if orientation == 1 else "退料"))
            self.motor_bus.run(fileament_id, orientation, total_ms,
                               release=True,
                               owner="move-ch%s" % fileament_id)
            return True

        # ---------------- 正常模式：带到位开关反馈 ----------------
        error_count = 0
        with mat.hold():
            for n in range(counts):
                self.motor_bus.motor.run(orientation, mat.step_ms)
                self.motor_bus.motor.stop()
                if mat.limit_triggered(orientation):
                    error_count += 1
            self.motor_bus.motor.stop()

        if error_count > counts * 0.2:
            return False
        return True

    # ======================================================================
    # 换料主流程
    # ======================================================================
    def exchange_fileament(self, new_filament_id, count=0):
        """执行一次换料：退掉旧料 → 送入新料。

        全程由 FilamentMotorBus 保证同一时刻只有 1 路电磁离合吸合。
        finally 里一定会停电机并断开全部离合，所以即使中途报错也不会
        出现"两路离合同时带电"的危险状态。
        """
        if new_filament_id not in self.dianji_dict:
            logout("换料失败：通道 %s 不存在" % new_filament_id, is_error=True)
            return False

        try:
            if count == 0:
                self.filament_current = self.now_filament(self.filament_current)

            logout("当前料盘:" + str(self.filament_current) + "新的料盘:" + str(new_filament_id))
            if self.filament_current == new_filament_id:
                logout("无需换料")
                return True

            # ---------- 通知打印机进入相对挤出模式 ----------
            self.piblish_gcode("M83")
            # 等打印机回应。用带超时的等待，打印机掉线时不会把程序挂死
            self.wait_msg_timeout(2000)
            time.sleep(0.1)

            # ---------- 步骤一：退料 ----------
            if self.filament_current:
                logout("开始退料")
                # 打印机自己先把喷嘴里的料退出来
                self.piblish_gcode("M400;\n G1 E-50 F200;")
                self.wait_msg_timeout(8000)
                logout(self.update_print_info())
                # AMS 侧再把料从挤出机/缓冲区收回到料盘
                if not self.fileament_move(self.filament_current,
                                           counts=RETRACT_STEPS, orientation=-1):
                    logout("退料失败", is_error=True)
                    return False
                # 退料动作彻底结束：停电机 + 断开全部离合
                self.motor_bus.release_all()
                self._check_bus()

            # ---------- 步骤二：进料 ----------
            logout("开始进料")
            mat_new = self.dianji_dict[new_filament_id]
            if mat_new.has_limit:
                # 有到位开关：反复推送，直到开关触发（fileament_move 返回 False）
                for n in range(LOAD_RETRY_TIMES):
                    logout("进料第 %d 次尝试" % (n + 1))
                    if not self.fileament_move(new_filament_id, orientation=1):
                        break
            else:
                # 无到位开关：只推一轮，时长由 NO_LIMIT_LOAD_MS 封顶
                self.fileament_move(new_filament_id,
                                    counts=LOAD_RETRY_TIMES, orientation=1)
            self.motor_bus.release_all()
            self._check_bus()

            # ---------- 步骤三：打印机拉料，AMS 辅助送料 ----------
            self.piblish_gcode("M400;\n G1 E50 F200;")
            self.wait_msg_timeout(8000)
            logout(self.update_print_info())
            mat_new.dianji_roll(1, LOAD_ASSIST_MS)

            # ---------- 步骤四：记录状态 ----------
            self.filament_current = new_filament_id
            self.save_current_filament()
            logout("换料成功")
            return True

        except ClutchConflictError as e:
            logout("换料中止：电磁离合状态异常 " + str(e), is_error=True)
            return False
        except MotorBusError as e:
            logout("换料中止：总线异常 " + str(e), is_error=True)
            return False
        except Exception as e:
            logout("换料异常:" + str(e), is_error=True)
            return False
        finally:
            # ★ 无论如何都要回到安全状态：电机停转、4 路离合全部断开
            try:
                self.motor_bus.release_all()
            except Exception:
                pass

    def _check_bus(self):
        """体检：确认没有多路离合同时吸合，返回 True 表示正常"""
        try:
            self.motor_bus.assert_single()
            return True
        except ClutchConflictError as e:
            logout("离合状态异常，已全部断开: " + str(e), is_error=True)
            return False

    # ======================================================================
    # 主循环
    # ======================================================================
    async def run_ams_loop(self):
        """主循环：轮询打印机状态 → 需要换料就换料 → 通知继续打印。

        ★ 性能关键：这里**绝不能**再出现阻塞式收包（带 await 名字的那种）。
          旧写法在没有消息时会把整个 uasyncio 事件循环按住，
          Web 配置页面和状态灯任务全被饿死 —— 就是"网页偶尔打不开"的元凶。
          现在统一用 poll_msg()（非阻塞，没消息立刻返回 None）。
        """
        exchange_count = 0        # 重复换料次数
        push_count = 0            # 重连计数（只用来少打点日志）
        last_push = time.ticks_ms()
        last_reconnect = time.ticks_ms()

        while True:
            try:
                # ---------------- 没连上 MQTT ----------------
                if not self.check_mqtt_connection():
                    push_count += 1
                    if push_count % 6 == 1:
                        logout("MQTT 未连接，尝试重连（第 %d 次）" % push_count)
                    self.conent_and_subscribe()
                    await asyncio.sleep(10)
                    continue
                push_count = 0

                # ---------------- 定期重建连接 ----------------
                # 旧代码是"每 20 轮重连一次"，而每轮都阻塞等消息，
                # 实际约等于每几秒就断一次 → 打印机侧看起来极不稳定。
                # 现在改成按时间：每 5 分钟才重建一次。
                now = time.ticks_ms()
                if time.ticks_diff(now, last_reconnect) >= RECONNECT_INTERVAL_MS:
                    last_reconnect = now
                    logout("定期重建 MQTT 连接")
                    try:
                        self.client.disconnect()
                    except Exception:
                        pass
                    await asyncio.sleep(1)
                    self.conent_and_subscribe()
                    continue

                # ★ 硬件体检：确保任何时刻最多只有 1 路电磁离合吸合
                try:
                    self.motor_bus.assert_single()
                except ClutchConflictError as e:
                    logout("离合体检异常: " + str(e), is_error=True)

                # ---------------- 定时向打印机查询状态 ----------------
                # 打印机自己也会主动上报，1 秒问一次足够了。
                if time.ticks_diff(now, last_push) >= PUSH_INTERVAL_MS:
                    last_push = now
                    self.piblish(START_PUSH)
                    #self.piblish(banbu_start)

                # ---------------- 非阻塞收包 ----------------
                if self.poll_msg() is None:
                    await asyncio.sleep_ms(AMS_POLL_MS)
                    continue

                # 只有真的收到新消息才去解析，否则会把上一条旧消息反复处理
                info = self.update_print_info()

                # ---------------- 判断是否需要换料 ----------------
                if info["change_info"]["code"] and exchange_count <= 5:
                    exchange_count += 1
                    if self.exchange_fileament(info["change_info"]["filament_next"] + 1, exchange_count):
                        exchange_count = 0
                        self.piblish(bambu_resume)   # 继续打印
                        for n in range(10):
                            await asyncio.sleep_ms(500)
                            if self.poll_msg() is None:
                                continue
                            try:
                                data = ujson.loads(self.new_message).get("print", {})
                            except Exception:
                                continue
                            if data.get("command", "") == "resume" and data.get("result", "") == "success":
                                logout("继续打印")
                                break
                if exchange_count > 3:
                    logout("AMS异常，已经暂停")

                await asyncio.sleep_ms(AMS_POLL_MS)

            except ClutchConflictError as e:
                logout("error:" + str(e), is_error=True)
                self.motor_bus.release_all()
                await asyncio.sleep(1)
            except Exception as e:
                logout("error:" + str(e))
                await asyncio.sleep(1)


if __name__ == "__main__":
    AMS_MODEL = AMS()
    print(AMS_MODEL.now_filament())
    #AMS_MODEL.auto_update_access("config.json")
    #print(AMS_MODEL.do_connect("Mr","15816728266"))
    #AMS_MODEL.conent_and_subscribe()
    #AMS_MODEL.piblish(START_PUSH)
    #AMS_MODEL.main_loop()
