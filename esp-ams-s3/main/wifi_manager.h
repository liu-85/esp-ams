/**
 * wifi_manager.h —— WiFi 联网与配网
 * ============================================================================
 *
 * 对应 MicroPython 版的 network_model.py。三条策略照搬，因为它们都是在真机上
 * 踩出来的：
 *
 * ----------------------------------------------------------------------------
 * 一、上电自动联网：**直连，不扫描**
 * ----------------------------------------------------------------------------
 *   scan() 是阻塞操作，一次要 1.5~3 秒。开机时先扫一遍再挑最强的连，
 *   会让开机慢、而且扫描期间射频要切信道，反而影响已经建立的连接。
 *
 *   首次配网成功后 SSID/密码已经在 NVS 里，"直接连"又快又稳。
 *   wifi_mgr_auto_connect() 只做一件事：拿记录里的 SSID/密码挨个尝试，
 *   全程不扫描。
 *
 *   想扫描只有一个入口：用户在网页上点「重新扫描」→ wifi_mgr_scan()。
 *
 * ----------------------------------------------------------------------------
 * 二、连接成功判定：必须拿到 IP
 * ----------------------------------------------------------------------------
 *   WIFI_EVENT_STA_CONNECTED 只表示"和 AP 关联上了"，此时 DHCP 可能还没完成。
 *   这时候去连 MQTT 必然失败。所以这里只认 IP_EVENT_STA_GOT_IP ——
 *   拿到 IP 才算连上，这一点和 Python 版 do_connect() 的判断一致。
 *
 * ----------------------------------------------------------------------------
 * 三、★ AP 开着的时候，STA 只能连同信道的路由器 ★
 * ----------------------------------------------------------------------------
 *   这是单射频芯片的硬约束（ESP32 系列只有一个射频单元，AP 和 STA 共用）。
 *   在 MicroPython 那版上实测过：SoftAP 开在信道 6，STA 去连一个信道 11 的
 *   路由器，就会一直卡在"连接中"出不来 IP；把 AP 一关，立刻连上。
 *
 *   所以 wifi_mgr_connect() 里做了一件 Python 版没做的事：
 *   **连接之前先查目标 AP 在哪个信道，把 SoftAP 也切到同一个信道**。
 *   这样 AP+STA 并存时也能连上，用户配网时不用手动关热点。
 *
 * ----------------------------------------------------------------------------
 * 四、省电模式必须关掉
 * ----------------------------------------------------------------------------
 *   ESP32 默认 pm=1（WIFI_PS_MIN_MODEM），射频空闲时会打盹。传大文件
 *   （65KB 的 index.html）时会中途掉关联，表现是"网页永远发不出去"。
 *
 *   这里在 wifi_mgr_start() 里显式调 esp_wifi_set_ps(WIFI_PS_NONE)。
 *   sdkconfig.defaults 里也配了一份（双保险）—— 这是整个项目里最容易复发
 *   的一类问题，值得重复设一次。
 */

#ifndef WIFI_MANAGER_H
#define WIFI_MANAGER_H

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>
#include "esp_err.h"
#include "freertos/FreeRTOS.h"
#include "freertos/event_groups.h"

#ifdef __cplusplus
extern "C" {
#endif

/* ==========================================================================
 * 事件位（给别的模块等 WiFi 用）
 * ========================================================================== */
#define WIFI_MGR_BIT_CONNECTED   BIT0   /**< STA 已关联并拿到 IP */
#define WIFI_MGR_BIT_AP_READY    BIT1   /**< SoftAP 已启动 */
#define WIFI_MGR_BIT_SCAN_DONE   BIT2   /**< 一次扫描已完成 */
#define WIFI_MGR_BIT_FAIL        BIT3   /**< 明确失败（密码错 / 找不到 AP） */

/** 一次扫描最多记多少个结果（够网页列一屏了，不做无限扫描） */
#define WIFI_MGR_SCAN_MAX 20
/** SSID 最长长度（含结尾 '\0'） */
#define WIFI_MGR_SSID_LEN 33

/* ==========================================================================
 * 生命周期
 * ========================================================================== */

/**
 * 初始化 WiFi。
 *
 * 做的事：建 netif → 建默认事件循环 → 注册事件处理器 → esp_wifi_init
 *         → **顺手关掉省电** → esp_wifi_start（射频真的起来）
 *
 * ⚠️ 必须在 app_main 里**尽早**调用，越早越好。
 *    ESP-IDF 不像 MicroPython 那样对堆碎片敏感（esp_wifi_init 用静态缓冲），
 *    但射频启动本身要花几百毫秒，早启动能让后面 MQTT/Web 的等待时间重叠掉。
 */
esp_err_t wifi_mgr_init(void);

/** 取事件组，别的模块可以 xEventGroupWaitBits() 等 WiFi 就绪 */
EventGroupHandle_t wifi_mgr_event_group(void);

/* ==========================================================================
 * STA（连路由器）
 * ========================================================================== */

/**
 * 连接指定 WiFi。**不阻塞**，只下发配置。
 * 连接结果通过事件位反馈（WIFI_MGR_BIT_CONNECTED / WIFI_MGR_BIT_FAIL）。
 *
 * ★ 发送配置前会做一件事：如果 SoftAP 正开着，先扫一下目标 SSID 在哪个
 *   信道，把 SoftAP 切到同一信道 —— 否则单射频下 STA 连不上（见文件头第三条）。
 *
 * @return ESP_OK 已下发；ESP_ERR_INVALID_ARG 参数为空
 */
esp_err_t wifi_mgr_connect(const char *ssid, const char *pass);

/**
 * 上电自动联网：拿 NVS 里保存的记录挨个直连，**不扫描**。
 *
 * @param timeout_ms 每组最长等多久拿到 IP
 * @return true 连上了
 */
bool wifi_mgr_auto_connect(uint32_t timeout_ms);

/** 断开 STA（保留配置） */
void wifi_mgr_disconnect(void);

/** 是否已连接并拿到 IP */
bool wifi_mgr_is_connected(void);

/** STA 的 IP；没连上返回 "" */
const char *wifi_mgr_sta_ip(void);

/** 当前连着的 SSID；没连上返回 "" */
const char *wifi_mgr_current_ssid(void);

/** 信号强度 dBm；没连上返回 0 */
int wifi_mgr_rssi(void);

/** MAC 地址字符串，形如 "b4:3a:45:a6:09:b8"；失败返回 "" */
const char *wifi_mgr_sta_mac(void);

/**
 * 把 status 码翻成中文（"密码错误" / "找不到该 WiFi" …）。
 * 对应 Python 版 network_model.status_text()，连"不要直接引用
 * network.STAT_CONNECT_FAIL 这种属性"的教训都照搬了 —— 这里改用
 * wifi_err_reason_t 的 switch，编译器会把缺的分支报出来，比查字典安全。
 */
const char *wifi_mgr_status_text(void);

/* ==========================================================================
 * AP（配置热点）
 * ========================================================================== */

/**
 * 打开配置热点。
 *
 * SSID / 密码 / 信道从 config 读取（默认 AMS_WIFI / A12345678 / 信道 6）。
 *
 * ⚠️ 幂等：已经开着就只更新信道（如果需要），**不会**重复下发完整配置。
 *    Python 版在碎堆上遇到过 "热点已 active 时再 config() 必定抛
 *    ESP_ERR_NO_MEM" 的坑；ESP-IDF 这边不存在内存问题，但幂等语义本身
 *    是对的 —— 避免"重启热点"把已经连上的客户端踢掉。
 */
esp_err_t wifi_mgr_ap_start(void);

/** 关闭配置热点 */
esp_err_t wifi_mgr_ap_stop(void);

/** 热点是否开着 */
bool wifi_mgr_ap_is_on(void);

/** 热点的 IP（通常 192.168.4.1）；没开返回 "" */
const char *wifi_mgr_ap_ip(void);

/** 当前连在热点上的客户端数量 */
int wifi_mgr_ap_client_count(void);

/* ==========================================================================
 * 扫描
 * ========================================================================== */

typedef struct {
    char    ssid[WIFI_MGR_SSID_LEN];
    int8_t  rssi;
    uint8_t channel;
    uint8_t authmode;
} wifi_mgr_ap_info_t;

/**
 * 扫描附近 WiFi。**阻塞**，通常 1.5~3 秒。
 *
 * @param out      结果数组
 * @param max      数组容量
 * @param timeout_ms 最长等待，0 用默认（5000）
 * @return 实际扫到几个；失败返回 -1
 *
 * ⚠️ 只应该在用户主动点「重新扫描」时调用 —— 每次刷新页面都扫一遍会明显卡顿。
 *    网页那边应该用 wifi_mgr_scan_cached()。
 */
int wifi_mgr_scan(wifi_mgr_ap_info_t *out, int max, uint32_t timeout_ms);

/**
 * 带缓存的扫描结果（缓存有效期 60 秒）。
 * force=true 强制重扫 —— 这就是网页上「重新扫描」按钮走的路。
 */
int wifi_mgr_scan_cached(wifi_mgr_ap_info_t *out, int max, bool force);

/**
 * 只取上一次扫描的缓存，**绝不触发扫描**（因此不会阻塞）。
 *
 * ★ 专供 /status 这类"每 2 秒被调一次"的接口用。
 *   绝不能在那种地方调 wifi_mgr_scan_cached() —— 缓存过期时它会真的扫一遍，
 *   每次刷新页面都卡 2~3 秒，正是老版本"网页很慢"的观感来源。
 *
 * @return 缓存条数；从开机到现在还没扫描过则返回 0（此时网页应提示"点重新扫描"）
 */
int wifi_mgr_scan_last(wifi_mgr_ap_info_t *out, int max);

/* ==========================================================================
 * mDNS
 * ========================================================================== */

/**
 * 在 WiFi 连上之后启动 mDNS，让用户不用记 IP：
 *     浏览器直接打开 http://ams.local
 *
 * 失败不影响主流程（只是少了个方便入口），返回错误码供日志记录。
 */
esp_err_t wifi_mgr_mdns_start(void);

#ifdef __cplusplus
}
#endif

#endif /* WIFI_MANAGER_H */
