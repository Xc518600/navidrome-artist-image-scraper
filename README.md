# Navidrome Artist Image Dashboard

一个面向 `Navidrome` 音乐库的长期运行 Web 控制台，支持：

- 歌手头像刮削与导出
- 缺失歌词识别与手动补写
- 缺失专辑图识别与在线补图
- 适配飞牛 NAS / Docker Compose 的常驻部署方式

仓库地址：`https://github.com/Xc518600/navidrome-artist-image-scraper`

镜像地址：`ghcr.io/xc518600/navidrome-artist-image-scraper:latest`

## 当前功能

### 1. 歌手头像

- 扫描音乐库中的歌手与目录
- 为专辑目录写入 `artist.*`
- 成功后可自动导出到 `Navidrome` 专用歌手头像目录
- 支持歌手别名归一
- 支持手动单个歌手补刮
- 待处理歌手名单会保留失败歌手，方便继续处理

### 2. 歌词刮削

- 统计总歌曲数 / 有歌词数 / 无歌词数
- 自动识别缺失歌词的歌曲
- 支持单首歌曲手动在线刮削
- 如果自动匹配失败，会展示候选歌词列表
- 候选项支持显示：
  - 歌曲名
  - 歌手名
  - 专辑名
  - 来源（`lrclib` / `netease`）
  - 歌词预览
- 可手动选择某条候选歌词写入当前歌曲

### 3. 专辑图刮削

- 识别缺失 `cover.jpg` 的目录
- 支持在线补专辑图
- 支持从内嵌封面或 MTW 数据补图
- 对混合目录做保护，避免误写整个目录的统一封面

## 部署方式

### 直接使用 GHCR 镜像

默认镜像：

```text
ghcr.io/xc518600/navidrome-artist-image-scraper:latest
```

启动：

```bash
docker compose pull
docker compose up -d
```

### 本地构建

```bash
docker compose -f docker-compose.build.yml build
docker compose -f docker-compose.build.yml up -d
```

## 飞牛 NAS 示例

默认 `docker-compose.yml` 已写死以下参数：

- 时区：`Asia/Shanghai`
- Web 端口：`38080`
- 音乐目录：`/vol4/1000/硬盘4国产电视剧/音乐`
- `music_tag_web` 配置目录：`/vol1/1000/docker/Music/music_tag_web/config`

浏览器访问：

```text
http://NAS_IP:38080
```

## 代理版部署

如果你的环境直连外网不稳定，可以使用代理版：

```bash
docker compose -f docker-compose.proxy.yml pull
docker compose -f docker-compose.proxy.yml up -d
```

当前代理版默认变量：

- `PROXY_HOST=http://192.168.3.30:20171`
- `HTTP_PROXY=http://192.168.3.30:20171`
- `HTTPS_PROXY=http://192.168.3.30:20171`

## 目录与持久化

需要持久化的主要目录：

- `./config`
- `./cache`

如果启用了 Navidrome 歌手头像集中导出，导出目录默认在：

- `./config/navidrome-artist-images`

## 主要配置项

### `artist_aliases`

用于把同一歌手的不同写法并到统一标准名。

示例：

```json
"artist_aliases": {
  "周杰倫": "周杰伦",
  "邓紫棋": "G.E.M. 邓紫棋",
  "G.E.M.邓紫棋": "G.E.M. 邓紫棋",
  "张韶涵": "Angela Chang"
}
```

### `overrides`

用于手工指定某个歌手的图片来源。

示例：

```json
"overrides": {
  "周杰伦": {
    "url": "https://example.com/jay.jpg"
  },
  "G.E.M. 邓紫棋": {
    "file": "/absolute/path/to/gem.jpg"
  }
}
```

### `navidrome_export_dir`

用于给 `Navidrome` 额外导出集中式歌手头像目录。

### `navidrome_artist_aliases`

为同一歌手额外导出多个命名版本，便于 `Navidrome` 命中。

### `flat_mixed_dir_names`

用于标记像 `自己下载` 这种“单目录平铺多歌手”的混合目录，避免错误地给整个目录统一写图。

## 当前首页入口

当前首页只保留 3 个主入口：

- `歌手头像`
- `刮削歌词`
- `刮削专辑图`

## 关键文件

```text
navidrome-artist-image-scraper/
├── Dockerfile
├── docker-compose.yml
├── docker-compose.build.yml
├── docker-compose.proxy.yml
├── docker-entrypoint.sh
├── config.example.json
├── scrape_artist_images.py
├── webapp.py
├── templates/index.html
├── static/style.css
└── README.md
```

## 发布镜像

### 登录 GHCR

```bash
echo '你的GitHub_TOKEN' | docker login ghcr.io -u xc518600 --password-stdin
```

### 构建并推送

```bash
docker build -t ghcr.io/xc518600/navidrome-artist-image-scraper:latest .
docker push ghcr.io/xc518600/navidrome-artist-image-scraper:latest
```

## 说明

- 这个项目不会直接修改 `Navidrome` 数据库
- 主要通过文件层面补充：
  - `artist.*`
  - `cover.jpg`
  - 歌词嵌入 / sidecar `.lrc`
- 更适合 NAS 常驻运行和手动处理长尾失败项
