# 继续打印
bambu_resume = '{"print":{"command":"resume","sequence_id":"1111111"},"user_id":"1"}'
# 退料
bambu_unload = '{"print":{"command":"ams_change_filament","curr_temp":220,"sequence_id":"1111111","tar_temp":220,"target":255},"user_id":"1"}'
# 进料
bambu_load = '{"print":{"command":"ams_change_filament","curr_temp":220,"sequence_id":"1111111","tar_temp":220,"target":254},"user_id":"1"}'
# 确定进料完成
bambu_done = '{"print":{"command":"ams_control","param":"done","sequence_id":"1111111"},"user_id":"1"}'
# 获取设备信息
banbu_start = '{"pushing": {"sequence_id": "1111111", "command": "pushall"}}'
# 开始推送
START_PUSH = '{ "pushing": {"sequence_id": "1111111", "command": "start"}}'
