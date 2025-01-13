from bambu.bambu_const import CURRENT_STAGE_IDS, HMS_ERRORS_ENGLISH

# 获取换料信息
def get_is_change_ams(data):
    gcode_state = data.get("gcode_state",None)
    mc_percent = data.get("mc_percent",None)
    mc_remaining_time = data.get("mc_remaining_time",None)
    if gcode_state == "PAUSE" and mc_percent == 101:
        return {"code":True,"filament_next":mc_remaining_time}
    return {"code":False,"filament_next":-1}
    
# 获取打印机运行状态
def get_status(data):
    relust =  {"code":None,"info":None}
    print_type = data.get("print_type",None)
    stg_cur = data.get("stg_cur",None)
    if print_type=="idle" and stg_cur==0:
        stg_cur=255
    if stg_cur:
        relust["info"] = CURRENT_STAGE_IDS[int(stg_cur)]
    else:
        relust["info"] = None
    relust["code"] = stg_cur
    return relust

# 获取错误码
def get_hms_code(hms_list_dict):
    attr = int(hms_list_dict["attr"])
    code = int(hms_list_dict["code"])
    if attr > 0 and code > 0:
        return '{:0>4X}_{:0>4X}_{:0>4X}_{:0>4X}'.format(int(attr/0x10000), attr & 0xFFFF, int(code / 0x10000), code & 0xFFFF)
    return ""

# 获取错误信息
def get_HMS_info(data):
    hms_error_list = []
    if "hms" in data:
        hmsList = data.get('hms', [])
        
        for hms_list_dict in hmsList:
            hms_code = get_hms_code(hms_list_dict)
            hms_error_info={"code":hms_code,"info":HMS_ERRORS_ENGLISH.get(hms_code,"unknow")}
            hms_error_list.append(hms_error_info)
    return hms_error_list

# 获取打印错误码
def get_print_error(data):
    print_error = data.get("print_error",None)
    return print_error
def get_nozzle_temper(data):
    nozzle_temper = data.get("nozzle_temper",0)
    return nozzle_temper
    