"""
boot.py —— 上电后最先执行的一段
================================

MicroPython 上电时会依次执行 boot.py、main.py。
这里只做两件**必须抢在任何外设对象创建之前**完成的事：

    1. 把电机 / 离合 / LED 全部置到"不上电"的安全电平
       裸 GPIO 在初始化前是**浮空**的，H 桥输入浮空会导致输出状态不确定
       （可能自己转、可能上下管直通发热）。

    2. 记录本次的复位原因（欠压？看门狗？冷启动？）并累加启动次数
       写进 boot_stat.json，串口和网页都能看到。

⚠️ 这个文件必须保持"快，且绝不抛异常"：
   出任何问题只打印一行，绝不能挡住后面的 main.py。
"""

try:
    import reset_info

    reset_info.make_safe()        # ① 先让功率器件进入安全状态
    reset_info.capture()          # ② 再记录复位原因
    reset_info.record_boot()      # ③ 最后累加启动计数
    print(reset_info.boot_banner())
except Exception as _boot_error:  # noqa: BLE001 - 兜住一切，保证系统能起来
    print("boot.py 自检步骤出错（已忽略，继续启动）: %r" % (_boot_error,))
