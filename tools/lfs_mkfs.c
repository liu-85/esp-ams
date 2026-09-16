/*
 * lfs_mkfs.c —— 生成 / 校验 ESP32 内部文件系统镜像（littlefs v2）
 *
 * 为什么用 C 而不是纯 Python 生成：
 *   ESP32 上的 MicroPython v1.23 内置 littlefs 2.8（磁盘格式 2.1），启动时由
 *   ports/esp32/modules/_boot.py 直接 vfs.mount(bdev, "/") 挂载，挂载失败会走
 *   inisetup.check_bootsec() —— 只要首扇区不是 0xFF 就判定「文件系统损坏」并
 *   进入死循环。所以镜像必须与设备端 100% 同格式：
 *     - 链路：lfs_format / lfs_file_write，全部由 littlefs 2.8 原生代码完成
 *     - 参数：完全对齐 MicroPython 的 VfsLfs2.mkfs（见 extmod/vfs_lfs.c）
 *             readsize=32 progsize=32 lookahead=32 block_cycles=100
 *             name_max/file_max/attr_max 取 littlefs 默认值 255/2G-1/1022
 *   而 littlefs 2.9+ 的 Python 绑定会写「内联文件」（inline file），
 *   2.8 完全不认识该结构，因此不能用它生成。
 *
 * 编译（CI 用 gcc，本地可用 tcc 验证）：
 *   gcc -O2 -o lfs_mkfs lfs_mkfs.c lfs.c lfs_util.c
 *
 * 用法：
 *   lfs_mkfs create --out <镜像> [--block-size 4096] [--block-count 512] --manifest <清单>
 *   lfs_mkfs verify --img <镜像> [--block-size 4096] [--block-count 512] --manifest <清单>
 *   lfs_mkfs list   --img <镜像> [--block-size 4096] [--block-count 512]
 *
 * 清单格式（每行一条，制表符分隔，UTF-8）：
 *   D <制表符> 目标目录
 *   F <制表符> 本地源文件 <制表符> 镜像内目标路径
 */

#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "lfs.h"

#define PATH_BUF 512
#define CHUNK 512

/* ---------- 块设备：直接落在本地文件上 ---------- */

static FILE *g_bd;

static int bd_read(const struct lfs_config *c, lfs_block_t block,
                   lfs_off_t off, void *buffer, lfs_size_t size) {
    if (fseek(g_bd, (long)block * c->block_size + (long)off, SEEK_SET) != 0) {
        return LFS_ERR_IO;
    }
    if (fread(buffer, 1, size, g_bd) != size) {
        return LFS_ERR_IO;
    }
    return 0;
}

static int bd_prog(const struct lfs_config *c, lfs_block_t block,
                   lfs_off_t off, const void *buffer, lfs_size_t size) {
    if (fseek(g_bd, (long)block * c->block_size + (long)off, SEEK_SET) != 0) {
        return LFS_ERR_IO;
    }
    if (fwrite(buffer, 1, size, g_bd) != size) {
        return LFS_ERR_IO;
    }
    return 0;
}

static int bd_erase(const struct lfs_config *c, lfs_block_t block) {
    unsigned char fill[4096];
    memset(fill, 0xFF, sizeof(fill));
    if (c->block_size > sizeof(fill)) {
        return LFS_ERR_IO;
    }
    if (fseek(g_bd, (long)block * c->block_size, SEEK_SET) != 0) {
        return LFS_ERR_IO;
    }
    if (fwrite(fill, 1, c->block_size, g_bd) != c->block_size) {
        return LFS_ERR_IO;
    }
    return 0;
}

static int bd_sync(const struct lfs_config *c) {
    (void)c;
    return fflush(g_bd) == 0 ? 0 : LFS_ERR_IO;
}

/* ---------- 配置：与 MicroPython VfsLfs2 完全一致 ---------- */

static struct lfs_config g_cfg = {
    .read = bd_read,
    .prog = bd_prog,
    .erase = bd_erase,
    .sync = bd_sync,
    .read_size = 32,
    .prog_size = 32,
    .block_size = 4096,
    .block_count = 512,
    .cache_size = 128, /* MIN(block_size, 4 * MAX(read_size, prog_size)) */
    .lookahead_size = 32,
    .block_cycles = 100,
    .name_max = 255,
    .file_max = 2147483647,
    .attr_max = 1022,
};

/* ---------- 小工具 ---------- */

static void die(const char *msg, const char *detail) {
    fprintf(stderr, "错误: %s%s%s\n", msg, detail ? ": " : "", detail ? detail : "");
    exit(1);
}

static void fs_err(const char *what, int err) {
    fprintf(stderr, "错误: %s 失败 (lfs err %d)\n", what, err);
    exit(1);
}

/* 递归创建目录，遇到已存在不算错 */
static int mkdir_p(lfs_t *lfs, const char *path) {
    char tmp[PATH_BUF];
    size_t len = strlen(path);
    if (len == 0) {
        return 0;
    }
    if (len >= sizeof(tmp)) {
        return LFS_ERR_NAMETOOLONG;
    }
    memcpy(tmp, path, len + 1);
    if (tmp[len - 1] == '/') {
        tmp[--len] = '\0';
    }
    for (char *p = tmp + 1; *p; ++p) {
        if (*p == '/') {
            *p = '\0';
            int err = lfs_mkdir(lfs, tmp);
            if (err && err != LFS_ERR_EXIST) {
                return err;
            }
            *p = '/';
        }
    }
    int err = lfs_mkdir(lfs, tmp);
    return err == LFS_ERR_EXIST ? 0 : err;
}

/* 取出路径中的目录部分，如 "/a/b/c.py" -> "/a/b" */
static void dirname_of(const char *path, char *out, size_t out_size) {
    size_t len = strlen(path);
    if (len >= out_size) {
        len = out_size - 1;
    }
    memcpy(out, path, len);
    out[len] = '\0';
    char *slash = strrchr(out, '/');
    if (slash == NULL) {
        out[0] = '/';
        out[1] = '\0';
    } else if (slash == out) {
        out[1] = '\0';
    } else {
        *slash = '\0';
    }
}

static long file_size(FILE *f) {
    long cur = ftell(f);
    fseek(f, 0, SEEK_END);
    long size = ftell(f);
    fseek(f, cur, SEEK_SET);
    return size;
}

/* ---------- 清单读取 ---------- */

typedef struct {
    char src[PATH_BUF];
    char dst[PATH_BUF];
} pair_t;

static pair_t *g_files;
static size_t g_file_count;
static char **g_dirs;
static size_t g_dir_count;

static char *xstrdup(const char *s) {
    size_t n = strlen(s) + 1;
    char *p = malloc(n);
    if (p == NULL) {
        die("内存不足", NULL);
    }
    memcpy(p, s, n);
    return p;
}

static void manifest_push_dir(char *line) {
    g_dirs = realloc(g_dirs, sizeof(char *) * (g_dir_count + 1));
    g_dirs[g_dir_count] = xstrdup(line);
    g_dir_count++;
}

static void manifest_push_file(char *line) {
    char *tab = strchr(line, '\t');
    if (tab == NULL) {
        die("清单中 F 行缺少制表符", line);
    }
    *tab = '\0';
    g_files = realloc(g_files, sizeof(pair_t) * (g_file_count + 1));
    snprintf(g_files[g_file_count].src, PATH_BUF, "%s", line);
    snprintf(g_files[g_file_count].dst, PATH_BUF, "%s", tab + 1);
    g_file_count++;
}

static void load_manifest(const char *path) {
    FILE *f = fopen(path, "rb");
    if (f == NULL) {
        die("无法打开清单文件", path);
    }
    char line[PATH_BUF * 2];
    size_t lineno = 0;
    while (fgets(line, sizeof(line), f) != NULL) {
        lineno++;
        size_t len = strlen(line);
        while (len > 0 && (line[len - 1] == '\n' || line[len - 1] == '\r')) {
            line[--len] = '\0';
        }
        if (len == 0 || line[0] == '#') {
            continue;
        }
        if (line[0] == 'D' && line[1] == '\t') {
            manifest_push_dir(line + 2);
        } else if (line[0] == 'F' && line[1] == '\t') {
            manifest_push_file(line + 2);
        } else {
            fprintf(stderr, "警告: 清单第 %u 行格式无法识别，已跳过: %s\n",
                    (unsigned)lineno, line);
        }
    }
    fclose(f);
}

/* ---------- 递归统计镜像内条目 ---------- */

static int walk_count(lfs_t *lfs, const char *path, size_t *files, size_t *dirs) {
    lfs_dir_t dir;
    struct lfs_info info;
    if (lfs_dir_open(lfs, &dir, path) != 0) {
        return -1;
    }
    while (lfs_dir_read(lfs, &dir, &info) > 0) {
        if (strcmp(info.name, ".") == 0 || strcmp(info.name, "..") == 0) {
            continue;
        }
        if (info.type == LFS_TYPE_DIR) {
            char child[PATH_BUF];
            snprintf(child, sizeof(child), "%s%s%s", path,
                     strcmp(path, "/") == 0 ? "" : "/", info.name);
            (*dirs)++;
            walk_count(lfs, child, files, dirs);
        } else {
            (*files)++;
        }
    }
    lfs_dir_close(lfs, &dir);
    return 0;
}

static int list_recursive(lfs_t *lfs, const char *path) {
    lfs_dir_t dir;
    struct lfs_info info;
    if (lfs_dir_open(lfs, &dir, path) != 0) {
        return -1;
    }
    while (lfs_dir_read(lfs, &dir, &info) > 0) {
        if (strcmp(info.name, ".") == 0 || strcmp(info.name, "..") == 0) {
            continue;
        }
        char child[PATH_BUF];
        snprintf(child, sizeof(child), "%s%s%s", path,
                 strcmp(path, "/") == 0 ? "" : "/", info.name);
        if (info.type == LFS_TYPE_DIR) {
            printf("DIR  %s\n", child);
            list_recursive(lfs, child);
        } else {
            printf("%6ld  %s\n", (long)info.size, child);
        }
    }
    lfs_dir_close(lfs, &dir);
    return 0;
}

/* ---------- create：格式化并写入全部文件 ---------- */

static int cmd_create(const char *img) {
    g_bd = fopen(img, "w+b");
    if (g_bd == NULL) {
        die("无法创建镜像文件", img);
    }
    /* 先把整片区域填成 0xFF，等价于已擦除的闪存 */
    size_t total = (size_t)g_cfg.block_size * g_cfg.block_count;
    unsigned char fill[4096];
    memset(fill, 0xFF, sizeof(fill));
    for (size_t written = 0; written < total; written += sizeof(fill)) {
        size_t n = total - written < sizeof(fill) ? total - written : sizeof(fill);
        if (fwrite(fill, 1, n, g_bd) != n) {
            die("镜像填充失败", img);
        }
    }

    lfs_t lfs;
    int err = lfs_format(&lfs, &g_cfg);
    if (err) {
        fs_err("lfs_format", err);
    }
    err = lfs_mount(&lfs, &g_cfg);
    if (err) {
        fs_err("lfs_mount", err);
    }

    for (size_t i = 0; i < g_dir_count; i++) {
        int derr = mkdir_p(&lfs, g_dirs[i]);
        if (derr) {
            fs_err(g_dirs[i], derr);
        }
    }

    char buf[CHUNK];
    for (size_t i = 0; i < g_file_count; i++) {
        char dir[PATH_BUF];
        dirname_of(g_files[i].dst, dir, sizeof(dir));
        err = mkdir_p(&lfs, dir);
        if (err) {
            fs_err(dir, err);
        }

        FILE *src = fopen(g_files[i].src, "rb");
        if (src == NULL) {
            die("无法读取源文件", g_files[i].src);
        }
        long size = file_size(src);

        lfs_file_t file;
        err = lfs_file_open(&lfs, &file, g_files[i].dst,
                            LFS_O_WRONLY | LFS_O_CREAT | LFS_O_TRUNC);
        if (err) {
            fs_err(g_files[i].dst, err);
        }
        size_t done = 0;
        while (done < (size_t)size) {
            size_t n = fread(buf, 1, sizeof(buf), src);
            if (n == 0) {
                break;
            }
            lfs_ssize_t wrote = lfs_file_write(&lfs, &file, buf, n);
            if (wrote != (lfs_ssize_t)n) {
                die("写入镜像失败", g_files[i].dst);
            }
            done += n;
        }
        err = lfs_file_close(&lfs, &file);
        if (err) {
            fs_err("lfs_file_close", err);
        }
        fclose(src);
    }

    size_t nfiles = 0, ndirs = 0;
    walk_count(&lfs, "/", &nfiles, &ndirs);
    lfs_unmount(&lfs);
    fclose(g_bd);

    printf("已生成镜像: %s\n", img);
    printf("  写入文件 %u 个 / 目录 %u 个（镜像内实际统计：文件 %u / 目录 %u）\n",
           (unsigned)g_file_count, (unsigned)g_dir_count, (unsigned)nfiles, (unsigned)ndirs);
    printf("  参数: block_size=%u block_count=%u 总大小=%u 字节\n",
           (unsigned)g_cfg.block_size, (unsigned)g_cfg.block_count, (unsigned)total);

    if (g_file_count != nfiles) {
        fprintf(stderr, "错误: 镜像内文件数与清单不一致\n");
        return 1;
    }
    return 0;
}

/* ---------- verify：重新挂载，逐文件与源文件比对 ---------- */

static int cmd_verify(const char *img) {
    g_bd = fopen(img, "rb");
    if (g_bd == NULL) {
        die("无法打开镜像文件", img);
    }
    lfs_t lfs;
    int err = lfs_mount(&lfs, &g_cfg);
    if (err) {
        fs_err("lfs_mount", err);
    }

    size_t nfiles = 0, ndirs = 0;
    walk_count(&lfs, "/", &nfiles, &ndirs);

    size_t bad = 0;
    unsigned char b1[CHUNK], b2[CHUNK];
    for (size_t i = 0; i < g_file_count; i++) {
        struct lfs_info info;
        if (lfs_stat(&lfs, g_files[i].dst, &info) != 0) {
            printf("缺失: %s\n", g_files[i].dst);
            bad++;
            continue;
        }
        FILE *src = fopen(g_files[i].src, "rb");
        if (src == NULL) {
            die("无法读取源文件", g_files[i].src);
            continue;
        }
        long size = file_size(src);
        if ((lfs_size_t)size != info.size) {
            printf("大小不符: %s (镜像 %u / 源 %ld)\n", g_files[i].dst,
                   (unsigned)info.size, size);
            bad++;
            fclose(src);
            continue;
        }
        lfs_file_t file;
        if (lfs_file_open(&lfs, &file, g_files[i].dst, LFS_O_RDONLY) != 0) {
            printf("无法打开: %s\n", g_files[i].dst);
            bad++;
            fclose(src);
            continue;
        }
        size_t done = 0;
        while (done < (size_t)size) {
            size_t n = (size_t)size - done < CHUNK ? (size_t)size - done : CHUNK;
            size_t rs = fread(b1, 1, n, src);
            lfs_ssize_t rl = lfs_file_read(&lfs, &file, b2, n);
            if (rs != n || rl != (lfs_ssize_t)n || memcmp(b1, b2, n) != 0) {
                printf("内容不符: %s (偏移 %u)\n", g_files[i].dst, (unsigned)done);
                bad++;
                break;
            }
            done += n;
        }
        lfs_file_close(&lfs, &file);
        fclose(src);
    }
    lfs_unmount(&lfs);
    fclose(g_bd);

    printf("校验结果: 文件 %u/%u 一致，目录 %u 个，镜像内文件总数 %u\n",
           (unsigned)(g_file_count - bad), (unsigned)g_file_count,
           (unsigned)ndirs, (unsigned)nfiles);
    if (bad != 0 || nfiles != g_file_count) {
        printf("校验失败\n");
        return 1;
    }
    printf("校验通过\n");
    return 0;
}

/* ---------- version：供构建脚本核对版本一致性 ---------- */

static int cmd_version(void) {
    printf("lfs_version=%u.%u\n", (unsigned)LFS_VERSION_MAJOR, (unsigned)LFS_VERSION_MINOR);
    printf("LFS_VERSION=0x%08x\n", (unsigned)LFS_VERSION);
    printf("LFS_DISK_VERSION=0x%08x\n", (unsigned)LFS_DISK_VERSION);
    return 0;
}

/* ---------- list ---------- */

static int cmd_list(const char *img) {    g_bd = fopen(img, "rb");
    if (g_bd == NULL) {
        die("无法打开镜像文件", img);
    }
    lfs_t lfs;
    int err = lfs_mount(&lfs, &g_cfg);
    if (err) {
        fs_err("lfs_mount", err);
    }
    list_recursive(&lfs, "/");
    lfs_unmount(&lfs);
    fclose(g_bd);
    return 0;
}

/* ---------- main ---------- */

static void usage(void) {
    fprintf(stderr,
            "用法:\n"
            "  lfs_mkfs version\n"
            "  lfs_mkfs create --out <镜像> [--block-size N] [--block-count N] --manifest <清单>\n"
            "  lfs_mkfs verify --img <镜像> [--block-size N] [--block-count N] --manifest <清单>\n"
            "  lfs_mkfs list   --img <镜像> [--block-size N] [--block-count N]\n");
    exit(2);
}

int main(int argc, char **argv) {
    if (argc < 2) {
        usage();
    }
    const char *mode = argv[1];
    const char *img = NULL;
    const char *manifest = NULL;

    if (strcmp(mode, "version") == 0) {
        return cmd_version();
    }

    for (int i = 2; i < argc; i++) {
        if (strcmp(argv[i], "--out") == 0 || strcmp(argv[i], "--img") == 0) {
            if (++i >= argc) {
                usage();
            }
            img = argv[i];
        } else if (strcmp(argv[i], "--manifest") == 0) {
            if (++i >= argc) {
                usage();
            }
            manifest = argv[i];
        } else if (strcmp(argv[i], "--block-size") == 0) {
            if (++i >= argc) {
                usage();
            }
            g_cfg.block_size = (lfs_size_t)strtoul(argv[i], NULL, 0);
        } else if (strcmp(argv[i], "--block-count") == 0) {
            if (++i >= argc) {
                usage();
            }
            g_cfg.block_count = (lfs_size_t)strtoul(argv[i], NULL, 0);
        } else {
            fprintf(stderr, "未知参数: %s\n", argv[i]);
            usage();
        }
    }

    if (img == NULL) {
        usage();
    }
    if (g_cfg.cache_size > g_cfg.block_size) {
        g_cfg.cache_size = g_cfg.block_size;
    }

    if (strcmp(mode, "create") == 0) {
        if (manifest == NULL) {
            usage();
        }
        load_manifest(manifest);
        return cmd_create(img);
    }
    if (strcmp(mode, "verify") == 0) {
        if (manifest == NULL) {
            usage();
        }
        load_manifest(manifest);
        return cmd_verify(img);
    }
    if (strcmp(mode, "list") == 0) {
        return cmd_list(img);
    }
    usage();
    return 2;
}
