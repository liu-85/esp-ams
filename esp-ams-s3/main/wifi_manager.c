/**
 * wifi_manager.c —— WiFi 联网与配网的实现
 * ============================================================================
 *
 * 结构上分成四块：
 *
 *   1. 事件处理  —— 只在这里改"连接状态"，其它模块读状态都是只读的
 *   2. STA 连接  —— 直连 + 单射频信道对齐
 *   3. SoftAP    —— 配网热点（幂等）
 *   4. 扫描      —— 带缓存，避免网页刷新时反复阻塞 3 秒
 *
 * ----------------------------------------------------------------------------
 * ★ 关于"单射频信道对齐"（这一段是本文件里最有价值的部分）
 * ----------------------------------------------------------------------------
 * ESP32 只有一个射频单元，AP 和 STA 共用。SoftAP 开着的时候，STA 只能关联到
 * **和 SoftAP 同信道**的 AP。
 *
 *   实测（MicroPython 版，同一块板子）：SoftAP 信道 6，去连信道 11 的路由器
 *   → 一直卡在 WIFI_ERR_REASON_CONNECT 状态出不来 IP；
 *   关掉 SoftAP → 同一个路由器立刻连上。
 *
 * 所以在 wifi_mgr_connect() 里做了对齐：先扫一次拿到目标 SSID 的信道，
 * 如果和当前 SoftAP 信道不同，就把 SoftAP 切过去，再让 STA 连。
 *
 * 代价是"连接前多扫一次"（约 2 秒），但这个开销只发生在用户点「连接」按钮
 * 的时候，不影响开机自动联网（那种情况下 SoftAP 通常没开，直接跳过对齐）。
 *
 * 注意：ESP-IDF 的 esp_wifi_set_config(WIFI_IF_AP, ...) 改信道时会**重启
 * SoftAP**，已连接的客户端会掉一次 —— 这个副作用可以接受（配网时客户端刚连上，
 * 重连即可），但如果 AP 上已经有客户端，我们宁可保持现状不动它，让用户手动
 * 关热点。下面的代码就是这么做的。
 */

#include "wifi_manager.h"

#include <stdio.h>
#include <string.h>

#include "esp_event.h"
#include "esp_log.h"
#include "esp_mac.h"
#include "esp_netif.h"
#include "esp_timer.h"
#include "esp_wifi.h"
#include "mdns.h"

#include "freertos/task.h"

#include "config.h"
#include "log_buffer.h"

/* ==========================================================================
 * 内部状态
 * ========================================================================== */
static EventGroupHandle_t s_events;
static esp_netif_t       *s_netif_sta;
static esp_netif_t       *s_netif_ap;

static volatile bool s_wifi_started;
static volatile bool s_ap_on;
static volatile int  s_last_disconnect_reason;

static char s_sta_ip[16];
static char s_ap_ip[16];
static char s_cur_ssid[WIFI_MGR_SSID_LEN];
static char s_sta_mac[18];
static int  s_rssi;

/* 本次连接的目标 SSID（事件回调里判断"是不是我们要连的那个"用） */
static char s_target_ssid[WIFI_MGR_SSID_LEN];

static wifi_mgr_ap_info_t s_scan_cache[WIFI_MGR_SCAN_MAX];
static int    s_scan_cache_count;
static int64_t s_scan_cache_us;

#define WIFI_SCAN_CACHE_US  (60LL * 1000 * 1000)   /* 60 秒 */

/* ==========================================================================
 * 一、事件处理
 * ========================================================================== */

static void update_sta_ip(void)
{
    esp_netif_ip_info_t info;
    s_sta_ip[0] = '\0';
    if (s_netif_sta && esp_netif_get_ip_info(s_netif_sta, &info) == ESP_OK &&
        info.ip.addr != 0) {
        snprintf(s_sta_ip, sizeof(s_sta_ip), IPSTR, IP2STR(&info.ip));
    }
}

static void update_ap_ip(void)
{
    esp_netif_ip_info_t info;
    s_ap_ip[0] = '\0';
    if (s_netif_ap && esp_netif_get_ip_info(s_netif_ap, &info) == ESP_OK) {
        snprintf(s_ap_ip, sizeof(s_ap_ip), IPSTR, IP2STR(&info.ip));
    }
}

static void on_wifi_event(void *arg, esp_event_base_t base,
                          int32_t event_id, void *event_data)
{
    (void)arg;
    (void)base;

    switch (event_id) {
    case WIFI_EVENT_STA_START:
        ams_log("STA 接口已启动");
        break;

    case WIFI_EVENT_STA_CONNECTED: {
        /* ★ 只是"关联上"，还没有 IP。WiFi 列表里能看到，
         *   但这时候去连 MQTT 一定失败 —— 所以这里**不置** CONNECTED 位。 */
        wifi_event_sta_connected_t *e =
            (wifi_event_sta_connected_t *)event_data;
        snprintf(s_cur_ssid, sizeof(s_cur_ssid), "%.*s",
                 (int)sizeof(e->ssid), (const char *)e->ssid);
        ams_log("WiFi 已关联: %s（信道 %d），等待获取 IP…",
                s_cur_ssid, e->channel);
        break;
    }

    case WIFI_EVENT_STA_DISCONNECTED: {
        wifi_event_sta_disconnected_t *e =
            (wifi_event_sta_disconnected_t *)event_data;
        s_last_disconnect_reason = e->reason;
        s_rssi = 0;
        s_sta_ip[0] = '\0';
        xEventGroupClearBits(s_events, WIFI_MGR_BIT_CONNECTED);

        /* 明确失败的原因：密码错 / 找不到 AP。这两个不用重试，早点告诉用户，
         * 免得界面上一直显示"连接中"让人干等 */
        bool hard_fail =
            (e->reason == WIFI_REASON_AUTH_FAIL ||
             e->reason == WIFI_REASON_NO_AP_FOUND ||
             e->reason == WIFI_REASON_HANDSHAKE_TIMEOUT ||
             e->reason == WIFI_REASON_ASSOC_FAIL);

        ams_log_err("WiFi 已断开（%s）：%s",
                    e->ssid[0] ? (const char *)e->ssid : "当前网络",
                    wifi_mgr_status_text());
        if (hard_fail) {
            xEventGroupSetBits(s_events, WIFI_MGR_BIT_FAIL);
        }
        break;
    }

    case WIFI_EVENT_AP_START:
        s_ap_on = true;
        update_ap_ip();
        xEventGroupSetBits(s_events, WIFI_MGR_BIT_AP_READY);
        break;

    case WIFI_EVENT_AP_STOP:
        s_ap_on = false;
        s_ap_ip[0] = '\0';
        xEventGroupClearBits(s_events, WIFI_MGR_BIT_AP_READY);
        break;

    case WIFI_EVENT_AP_STACONNECTED: {
        wifi_event_ap_staconnected_t *e =
            (wifi_event_ap_staconnected_t *)event_data;
        ams_log("有设备连上配置热点: " MACSTR, MAC2STR(e->mac));
        break;
    }

    case WIFI_EVENT_AP_STADISCONNECTED: {
        wifi_event_ap_stadisconnected_t *e =
            (wifi_event_ap_stadisconnected_t *)event_data;
        ams_log("设备已离开配置热点: " MACSTR, MAC2STR(e->mac));
        break;
    }

    case WIFI_EVENT_SCAN_DONE:
        xEventGroupSetBits(s_events, WIFI_MGR_BIT_SCAN_DONE);
        break;

    default:
        break;
    }
}

static void on_ip_event(void *arg, esp_event_base_t base,
                        int32_t event_id, void *event_data)
{
    (void)arg;
    (void)base;
    (void)event_data;

    if (event_id != IP_EVENT_STA_GOT_IP) {
        return;
    }
    update_sta_ip();
    s_rssi = wifi_mgr_rssi();

    xEventGroupClearBits(s_events, WIFI_MGR_BIT_FAIL);
    xEventGroupSetBits(s_events, WIFI_MGR_BIT_CONNECTED);

    ams_log("WiFi 连接成功: %s  IP=%s  信号=%ddBm",
            s_cur_ssid, s_sta_ip, s_rssi);

    /* 拿到 IP 才启动 mDNS —— 它的广告要用 STA 的地址 */
    wifi_mgr_mdns_start();
}

/* ==========================================================================
 * 二、初始化
 * ========================================================================== */

esp_err_t wifi_mgr_init(void)
{
    if (s_events == NULL) {
        s_events = xEventGroupCreate();
    }

    ESP_ERROR_CHECK(esp_netif_init());

    /* 默认事件循环：允许它已经存在（比如被别的组件先建了） */
    esp_err_t err = esp_event_loop_create_default();
    if (err != ESP_OK && err != ESP_ERR_INVALID_STATE) {
        ams_log_err("创建默认事件循环失败: %s", esp_err_to_name(err));
        return err;
    }

    s_netif_sta = esp_netif_create_default_wifi_sta();
    s_netif_ap  = esp_netif_create_default_wifi_ap();
    if (!s_netif_sta || !s_netif_ap) {
        ams_log_err("创建 WiFi netif 失败");
        return ESP_FAIL;
    }

    wifi_init_config_t init_cfg = WIFI_INIT_CONFIG_DEFAULT();
    err = esp_wifi_init(&init_cfg);
    if (err != ESP_OK) {
        ams_log_err("esp_wifi_init 失败: %s", esp_err_to_name(err));
        return err;
    }

    ESP_ERROR_CHECK(esp_event_handler_instance_register(
        WIFI_EVENT, ESP_EVENT_ANY_ID, &on_wifi_event, NULL, NULL));
    ESP_ERROR_CHECK(esp_event_handler_instance_register(
        IP_EVENT, IP_EVENT_STA_GOT_IP, &on_ip_event, NULL, NULL));

    /* STA 配置：默认就自动重连，失联了驱动自己会试 */
    wifi_config_t sta_cfg = {0};
    sta_cfg.sta.threshold.authmode = WIFI_AUTH_WPA2_PSK;
    sta_cfg.sta.pmf_cfg.capable = true;
    sta_cfg.sta.pmf_cfg.required = false;
    esp_wifi_set_config(WIFI_IF_STA, &sta_cfg);

    /* 先用 STA 模式启动（绝大多数时间都只需要 STA）。
     * 要开热点时再切 APSTA —— 见 wifi_mgr_ap_start()。 */
    ESP_ERROR_CHECK(esp_wifi_set_mode(WIFI_MODE_STA));
    ESP_ERROR_CHECK(esp_wifi_start());
    s_wifi_started = true;

    /* ★★ 关掉省电 ★★
     * 这一行是整个项目里"网页能不能发出去"的关键。ESP32 默认 pm=1，
     * 射频空闲时打盹，传大文件中途会掉关联。sdkconfig.defaults 里也配了
     * CONFIG_ESP_WIFI_POWER_SAVE_NONE=y，这里是第二道保险 —— 这种问题
     * 复发一次就要排查一整天，值得重复设一遍。 */
    esp_wifi_set_ps(WIFI_PS_NONE);

    /* 记下 STA 的 MAC，网页上显示出来方便在路由器后台找设备 */
    uint8_t mac[6] = {0};
    if (esp_wifi_get_mac(WIFI_IF_STA, mac) == ESP_OK) {
        snprintf(s_sta_mac, sizeof(s_sta_mac), MACSTR, MAC2STR(mac));
    }

    ams_log("WiFi 驱动已启动（省电已关闭，MAC=%s）", s_sta_mac);
    return ESP_OK;
}

EventGroupHandle_t wifi_mgr_event_group(void)
{
    return s_events;
}

/* ==========================================================================
 * 三、STA 连接
 * ========================================================================== */

const char *wifi_mgr_sta_ip(void)
{
    return s_sta_ip;
}

bool wifi_mgr_is_connected(void)
{
    return s_sta_ip[0] != '\0';
}

const char *wifi_mgr_current_ssid(void)
{
    return s_cur_ssid;
}

const char *wifi_mgr_sta_mac(void)
{
    return s_sta_mac;
}

int wifi_mgr_rssi(void)
{
    wifi_ap_record_t ap;
    if (esp_wifi_sta_get_ap_info(&ap) == ESP_OK) {
        return ap.rssi;
    }
    return 0;
}

const char *wifi_mgr_status_text(void)
{
    switch (s_last_disconnect_reason) {
    case WIFI_REASON_NO_AP_FOUND:            return "找不到该 WiFi";
    case WIFI_REASON_AUTH_FAIL:              return "密码错误";
    case WIFI_REASON_ASSOC_FAIL:             return "关联被拒绝";
    case WIFI_REASON_HANDSHAKE_TIMEOUT:      return "握手超时（密码或加密方式不符）";
    case WIFI_REASON_CONNECTION_FAIL:        return "连接失败";
    case WIFI_REASON_BEACON_TIMEOUT:         return "信号丢失（基站超时）";
    case WIFI_REASON_4WAY_HANDSHAKE_TIMEOUT: return "四次握手超时";
    case WIFI_REASON_NO_AP_FOUND_W_COMPATIBLE_SECURITY:
        return "找到了该 WiFi，但加密方式不兼容";
    case 0:                                  return "未连接";
    default:
        /* 把原始码也带上 —— 查不到的原因交给用户去搜，比只说"未知"有用 */
        {
            static char buf[48];
            snprintf(buf, sizeof(buf), "断开（原因码 %d）",
                     s_last_disconnect_reason);
            return buf;
        }
    }
}

/**
 * 单射频信道对齐：如果 SoftAP 开着且信道与目标不同，把 SoftAP 切过去。
 *
 * 只在"热点上没有客户端"时才切 —— 有客户端的时候切信道会把人家踢下来，
 * 那种情况宁可保持现状，由用户决定（配网页面上有"关闭热点"按钮）。
 */
static void align_ap_channel_to(const char *ssid)
{
    if (!s_ap_on || ssid == NULL || ssid[0] == '\0') {
        return;
    }

    int target_ch = -1;
    wifi_mgr_ap_info_t list[WIFI_MGR_SCAN_MAX];
    int n = wifi_mgr_scan_cached(list, WIFI_MGR_SCAN_MAX, false);
    for (int i = 0; i < n; i++) {
        if (strncmp(list[i].ssid, ssid, WIFI_MGR_SSID_LEN) == 0) {
            target_ch = list[i].channel;
            break;
        }
    }
    if (target_ch <= 0) {
        return;   /* 没扫到（可能不在范围），交给驱动自己处理 */
    }

    wifi_config_t ap_cfg;
    if (esp_wifi_get_config(WIFI_IF_AP, &ap_cfg) != ESP_OK) {
        return;
    }
    if (ap_cfg.ap.channel == (uint8_t)target_ch) {
        return;
    }

    /* 热点上挂着客户端时不动它 */
    wifi_sta_list_t sta_list;
    if (esp_wifi_ap_get_sta_list(&sta_list) == ESP_OK &&
        sta_list.num > 0) {
        ams_log_warn("配置热点上有 %d 个设备在用，暂不改信道"
                     "（单射频下 STA 只能连同信道的路由器；"
                     "如果连不上 %s，请先关掉热点）",
                     sta_list.num, ssid);
        return;
    }

    ap_cfg.ap.channel = (uint8_t)target_ch;
    esp_err_t err = esp_wifi_set_config(WIFI_IF_AP, &ap_cfg);
    if (err == ESP_OK) {
        ams_log("为连上 %s（信道 %d），已把配置热点切到同一信道", ssid,
                target_ch);
    } else {
        ams_log_warn("切热点信道失败: %s", esp_err_to_name(err));
    }
}

esp_err_t wifi_mgr_connect(const char *ssid, const char *pass)
{
    if (!ssid || ssid[0] == '\0') {
        return ESP_ERR_INVALID_ARG;
    }
    if (!s_wifi_started) {
        return ESP_ERR_INVALID_STATE;
    }

    /* 先把状态清干净：上一轮的失败标志和连接标志都不应该参与这一次判断 */
    xEventGroupClearBits(s_events, WIFI_MGR_BIT_CONNECTED | WIFI_MGR_BIT_FAIL);
    s_last_disconnect_reason = 0;

    strncpy(s_target_ssid, ssid, sizeof(s_target_ssid) - 1);
    s_target_ssid[sizeof(s_target_ssid) - 1] = '\0';

    /* ★ 单射频对齐（见文件头第三条） */
    align_ap_channel_to(ssid);

    wifi_config_t cfg = {0};
    strncpy((char *)cfg.sta.ssid, ssid, sizeof(cfg.sta.ssid) - 1);
    if (pass) {
        strncpy((char *)cfg.sta.password, pass, sizeof(cfg.sta.password) - 1);
    }
    cfg.sta.threshold.authmode =
        (pass && pass[0]) ? WIFI_AUTH_WPA2_PSK : WIFI_AUTH_OPEN;

    /* 主动重连：驱动会在断开后自己按这个间隔重试（次数 -1 表示一直试） */
    cfg.sta.scan_method = WIFI_ALL_CHANNEL_SCAN;
    cfg.sta.sort_method = WIFI_CONNECT_AP_BY_SIGNAL;

    esp_err_t err = esp_wifi_set_config(WIFI_IF_STA, &cfg);
    if (err != ESP_OK) {
        ams_log_err("下发 WiFi 配置失败: %s", esp_err_to_name(err));
        return err;
    }

    /* 已经连着别的网络就先断开，让这次连接从干净状态开始 */
    if (wifi_mgr_is_connected()) {
        esp_wifi_disconnect();
        vTaskDelay(pdMS_TO_TICKS(200));
    }

    ams_log("正在连接 WiFi: %s …", ssid);
    err = esp_wifi_connect();
    if (err != ESP_OK) {
        ams_log_err("调用 esp_wifi_connect 失败: %s", esp_err_to_name(err));
        return err;
    }
    return ESP_OK;
}

void wifi_mgr_disconnect(void)
{
    if (s_wifi_started) {
        esp_wifi_disconnect();
    }
    s_sta_ip[0] = '\0';
    s_cur_ssid[0] = '\0';
    if (s_events) {
        xEventGroupClearBits(s_events, WIFI_MGR_BIT_CONNECTED);
    }
}

bool wifi_mgr_auto_connect(uint32_t timeout_ms)
{
    if (wifi_mgr_is_connected()) {
        return true;
    }

    ams_config_t *cfg = config_get();
    if (cfg->profile_count == 0) {
        ams_log("没有已保存的 WiFi 记录，直接进入配网模式");
        return false;
    }

    ams_log("已保存 %u 个 WiFi 记录，按顺序直连（不扫描）",
            (unsigned)cfg->profile_count);

    for (int i = 0; i < cfg->profile_count; i++) {
        const config_wifi_profile_t *p = &cfg->profiles[i];
        if (p->ssid[0] == '\0') {
            continue;
        }
        ams_log("尝试 (%d/%u): %s", i + 1, (unsigned)cfg->profile_count,
                p->ssid);

        xEventGroupClearBits(s_events, WIFI_MGR_BIT_CONNECTED |
                                       WIFI_MGR_BIT_FAIL);
        if (wifi_mgr_connect(p->ssid, p->pass) != ESP_OK) {
            continue;
        }

        EventBits_t bits = xEventGroupWaitBits(
            s_events, WIFI_MGR_BIT_CONNECTED | WIFI_MGR_BIT_FAIL,
            pdFALSE, pdFALSE, pdMS_TO_TICKS(timeout_ms));

        if (bits & WIFI_MGR_BIT_CONNECTED) {
            /* ★ 把成功的这组挪到最前面，下次开机第一个就试它 */
            config_set_wifi(p->ssid, p->pass);
            return true;
        }
        if (bits & WIFI_MGR_BIT_FAIL) {
            ams_log_err("连接 %s 明确失败：%s", p->ssid,
                        wifi_mgr_status_text());
        } else {
            ams_log_err("连接 %s 超时（%ums）", p->ssid,
                        (unsigned)timeout_ms);
        }
        esp_wifi_disconnect();
        vTaskDelay(pdMS_TO_TICKS(300));
    }

    ams_log_err("全部 WiFi 记录都连不上，转由上层打开配置热点");
    return false;
}

/* ==========================================================================
 * 四、SoftAP
 * ========================================================================== */

bool wifi_mgr_ap_is_on(void)
{
    return s_ap_on;
}

const char *wifi_mgr_ap_ip(void)
{
    return s_ap_ip;
}

int wifi_mgr_ap_client_count(void)
{
    wifi_sta_list_t list;
    if (esp_wifi_ap_get_sta_list(&list) == ESP_OK) {
        return list.num;
    }
    return 0;
}

/**
 * 配置热点应该用哪个信道。
 *
 * ★ STA 已连上时，**必须**跟随 STA 的信道：
 *   ESP32 只有一个射频，AP 和 STA 分时复用。热点在信道 6、路由器在信道 11
 *   的话，射频要在两个信道之间来回跳 —— 结果是 STA 掉线、或者热点上的
 *   客户端连不稳。
 *
 *   这不是理论推导：在 MicroPython 那版上实测过，SoftAP 开在信道 6 时，
 *   STA 连信道 11 的路由器会一直卡在"连接中"出不来 IP，AP 一关立刻连上。
 *   所以这里主动对齐，让"STA 连上之后再开热点"这件事变得安全。
 */
static uint8_t desired_ap_channel(void)
{
    ams_config_t *cfg = config_get();
    uint8_t fallback = cfg->ap_channel ? cfg->ap_channel
                                       : CONFIG_DEFAULT_AP_CHANNEL;

    if (wifi_mgr_is_connected()) {
        wifi_ap_record_t cur_ap;
        if (esp_wifi_sta_get_ap_info(&cur_ap) == ESP_OK && cur_ap.primary > 0) {
            return cur_ap.primary;
        }
    }
    return fallback;
}

esp_err_t wifi_mgr_ap_start(void)
{
    ams_config_t *cfg = config_get();

    /* ★ 幂等：已经开着就只处理信道，不重复下发完整配置。
     *   （完整下发会重启 SoftAP，把已连上的客户端踢掉） */
    if (s_ap_on) {
        wifi_config_t cur;
        uint8_t want = desired_ap_channel();
        if (esp_wifi_get_config(WIFI_IF_AP, &cur) == ESP_OK &&
            cur.ap.channel != want) {
            if (wifi_mgr_ap_client_count() == 0) {
                cur.ap.channel = want;
                esp_wifi_set_config(WIFI_IF_AP, &cur);
                ams_log("配置热点信道已对齐到 %d", (int)want);
            } else {
                ams_log_warn("配置热点上有 %d 个设备在用，暂不改信道",
                             wifi_mgr_ap_client_count());
            }
        }
        ams_log("配置热点已在运行: %s", cfg->ap_ssid);
        return ESP_OK;
    }

    /* 切到 APSTA：配网时热点和"尝试连路由器"要能同时存在 */
    esp_err_t err = esp_wifi_set_mode(WIFI_MODE_APSTA);
    if (err != ESP_OK) {
        ams_log_err("切换到 APSTA 模式失败: %s", esp_err_to_name(err));
        return err;
    }

    wifi_config_t ap = {0};
    snprintf((char *)ap.ap.ssid, sizeof(ap.ap.ssid), "%s", cfg->ap_ssid);
    ap.ap.ssid_len = (uint8_t)strlen((char *)ap.ap.ssid);
    ap.ap.channel = desired_ap_channel();
    ap.ap.max_connection = 4;

    if (cfg->ap_pass[0] != '\0') {
        snprintf((char *)ap.ap.password, sizeof(ap.ap.password), "%s",
                 cfg->ap_pass);
        ap.ap.authmode = WIFI_AUTH_WPA2_PSK;
    } else {
        ap.ap.authmode = WIFI_AUTH_OPEN;
    }

    err = esp_wifi_set_config(WIFI_IF_AP, &ap);
    if (err != ESP_OK) {
        ams_log_err("配置热点参数下发失败: %s", esp_err_to_name(err));
        return err;
    }

    /* 注意：esp_wifi_start() 在 wifi_mgr_init() 里已经调过了，模式切换后
     * 热点会自己起来，不需要再 start 一次。 */
    ams_log("配置热点已打开: %s  密码: %s  信道: %d",
            cfg->ap_ssid,
            cfg->ap_pass[0] ? cfg->ap_pass : "(无密码)",
            (int)ap.ap.channel);
    ams_log("手机连上热点后，浏览器打开 http://%s 配网",
            s_ap_ip[0] ? s_ap_ip : "192.168.4.1");
    return ESP_OK;
}

esp_err_t wifi_mgr_ap_stop(void)
{
    if (!s_ap_on) {
        return ESP_OK;
    }
    /* 只切回 STA 模式，热点就没了 —— 不需要专门的 stop API */
    esp_err_t err = esp_wifi_set_mode(WIFI_MODE_STA);
    if (err != ESP_OK) {
        ams_log_err("关闭配置热点失败: %s", esp_err_to_name(err));
        return err;
    }
    ams_log("配置热点已关闭");
    return ESP_OK;
}

/* ==========================================================================
 * 五、扫描
 * ========================================================================== */

int wifi_mgr_scan(wifi_mgr_ap_info_t *out, int max, uint32_t timeout_ms)
{
    if (!out || max <= 0 || !s_wifi_started) {
        return -1;
    }
    if (timeout_ms == 0) {
        timeout_ms = 5000;
    }

    xEventGroupClearBits(s_events, WIFI_MGR_BIT_SCAN_DONE);

    wifi_scan_config_t scan = {
        .ssid        = NULL,
        .bssid       = NULL,
        .channel     = 0,        /* 0 = 全信道 */
        .show_hidden = false,
        .scan_type   = WIFI_SCAN_TYPE_ACTIVE,
    };

    esp_err_t err = esp_wifi_scan_start(&scan, false);
    if (err != ESP_OK) {
        /* 扫描期间如果 STA 正在连某台路由器，驱动会返回 ESP_ERR_WIFI_STATE。
         * 这是正常竞争，不是错误，静默返回即可 —— 上层会显示上一次的缓存。 */
        if (err != ESP_ERR_WIFI_STATE) {
            ams_log_err("启动 WiFi 扫描失败: %s", esp_err_to_name(err));
        }
        return -1;
    }

    EventBits_t bits = xEventGroupWaitBits(
        s_events, WIFI_MGR_BIT_SCAN_DONE, pdTRUE, pdFALSE,
        pdMS_TO_TICKS(timeout_ms));
    if (!(bits & WIFI_MGR_BIT_SCAN_DONE)) {
        ams_log_err("WiFi 扫描超时（%ums）", (unsigned)timeout_ms);
        esp_wifi_scan_stop();
        return -1;
    }

    uint16_t number = 0;
    esp_wifi_scan_get_ap_num(&number);
    if (number == 0) {
        return 0;
    }

    /* 最多取 WIFI_MGR_SCAN_MAX 条，避免栈上开大数组 */
    static wifi_ap_record_t records[WIFI_MGR_SCAN_MAX];
    uint16_t want = number > WIFI_MGR_SCAN_MAX ? WIFI_MGR_SCAN_MAX : number;
    if (esp_wifi_scan_get_ap_records(&want, records) != ESP_OK) {
        return -1;
    }

    int n = 0;
    for (int i = 0; i < want && n < max; i++) {
        if (records[i].ssid[0] == '\0') {
            continue;   /* 隐藏网络不列出来，没意义还占位 */
        }
        /* 同一个 SSID 可能有多条（多个 AP），只留信号最强的那条 */
        bool dup = false;
        for (int j = 0; j < n; j++) {
            if (strncmp(out[j].ssid, (const char *)records[i].ssid,
                        WIFI_MGR_SSID_LEN) == 0) {
                if (records[i].rssi > out[j].rssi) {
                    out[j].rssi = records[i].rssi;
                    out[j].channel = records[i].channel;
                }
                dup = true;
                break;
            }
        }
        if (dup) {
            continue;
        }
        snprintf(out[n].ssid, sizeof(out[n].ssid), "%s",
                 (const char *)records[i].ssid);
        out[n].rssi = records[i].rssi;
        out[n].channel = records[i].channel;
        out[n].authmode = (uint8_t)records[i].authmode;
        n++;
    }

    /* 按信号强度排序（简单插入排序 —— n 最多 20，不值得引入 qsort 的开销） */
    for (int i = 1; i < n; i++) {
        wifi_mgr_ap_info_t key = out[i];
        int j = i - 1;
        while (j >= 0 && out[j].rssi < key.rssi) {
            out[j + 1] = out[j];
            j--;
        }
        out[j + 1] = key;
    }

    ams_log("扫描到 %d 个 WiFi", n);
    return n;
}

int wifi_mgr_scan_cached(wifi_mgr_ap_info_t *out, int max, bool force)
{
    if (!out || max <= 0) {
        return -1;
    }

    int64_t now = esp_timer_get_time();
    if (!force && s_scan_cache_count > 0 &&
        (now - s_scan_cache_us) < WIFI_SCAN_CACHE_US) {
        int n = s_scan_cache_count < max ? s_scan_cache_count : max;
        memcpy(out, s_scan_cache, sizeof(wifi_mgr_ap_info_t) * n);
        return n;
    }

    int n = wifi_mgr_scan(s_scan_cache, WIFI_MGR_SCAN_MAX, 0);
    if (n >= 0) {
        s_scan_cache_count = n;
        s_scan_cache_us = now;
    } else {
        /* 扫失败就把缓存有效期延长一点，避免每次刷新都重试 3 秒 */
        s_scan_cache_us = now - WIFI_SCAN_CACHE_US / 2;
        n = s_scan_cache_count;
    }

    int copy = n < max ? n : max;
    if (copy > 0) {
        memcpy(out, s_scan_cache, sizeof(wifi_mgr_ap_info_t) * copy);
    }
    return copy;
}

int wifi_mgr_scan_last(wifi_mgr_ap_info_t *out, int max)
{
    if (!out || max <= 0) {
        return 0;
    }
    int n = s_scan_cache_count < max ? s_scan_cache_count : max;
    if (n > 0) {
        memcpy(out, s_scan_cache, sizeof(wifi_mgr_ap_info_t) * n);
    }
    return n > 0 ? n : 0;
}

/* ==========================================================================
 * 六、mDNS
 * ========================================================================== */
esp_err_t wifi_mgr_mdns_start(void)
{
    static bool started;
    if (started) {
        return ESP_OK;
    }

    esp_err_t err = mdns_init();
    if (err != ESP_OK) {
        ams_log_warn("mDNS 初始化失败(%s)，只能用 IP 访问",
                     esp_err_to_name(err));
        return err;
    }

    mdns_hostname_set("ams");
    mdns_instance_name_set("YAO AMS");
    mdns_service_add(NULL, "_http", "_tcp", 80, NULL, 0);
    started = true;

    ams_log("mDNS 已启动：浏览器可直接访问 http://ams.local");
    return ESP_OK;
}
