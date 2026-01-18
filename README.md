# NUIST 成绩更新通知

<div align="center">

![Python](https://img.shields.io/badge/Python-3.8+-blue?logo=python&logoColor=white)
![License](https://img.shields.io/badge/License-MIT-green)
![Platform](https://img.shields.io/badge/Platform-Windows%20%7C%20Linux%20%7C%20macOS-lightgrey)

</div>

本项目用于自动查询 NUIST 教务系统的成绩更新，并通过 OneBot 协议发送 QQ 通知。支持多用户配置、Cookies 缓存、GPA 自动获取等功能。

## 适用范围

理论上适用于所有使用**金智教务系统**的高校，但目前仅在 **南京信息工程大学 (NUIST)** 进行过测试。

## ✨ 功能特性

- 🔐 **自动登录** - 使用 Playwright 自动化登录，支持验证码自动识别
- 🍪 **Cookies 缓存** - 缓存登录状态，减少登录频率
- 📊 **成绩检测** - 自动检测新增成绩，避免重复通知
- 📈 **GPA 获取** - 自动获取最新 GPA 并随通知发送
- 📱 **QQ 通知** - 通过 OneBot 协议发送私聊消息
- 👥 **多用户支持** - 支持配置多个用户账号

## 📁 项目结构

```
cjcx/
├── main.py           # 主程序入口
├── NuistLogin.py     # 登录模块（Playwright + OCR）
├── requirements.txt  # 依赖清单
└── README.md         # 项目说明
```

## 🚀 快速开始

### 环境要求

- Python 3.8+
- OneBot 实现（如 [Lagrange.OneBot](https://github.com/LagrangeDev/Lagrange.Core)、[NapCat](https://github.com/NapNeko/NapCatQQ) 等）

### 安装依赖

```bash
# 安装 Python 依赖
pip install -r requirements.txt

# 安装 Playwright 浏览器
playwright install chromium
```

### 使用方法

#### 单次运行

```bash
python main.py -u <学号> -p <密码> -qq <QQ号>
```

#### 完整参数

```bash
python main.py -u <学号> -p <密码> -qq <QQ号> [--webhook <OneBot地址>] [--multi]
```

| 参数 | 必填 | 说明 | 默认值 |
|------|------|------|--------|
| `-u, --user` | ✅ | 教务系统学号 | - |
| `-p, --password` | ✅ | 教务系统密码 | - |
| `-qq, --qq` | ✅ | 接收通知的 QQ 号 | - |
| `--webhook` | ❌ | OneBot HTTP API 地址 | `http://127.0.0.1:3000/send_private_msg` |
| `--multi` | ❌ | 多用户模式（通知中显示学号） | `False` |

#### 定时任务配置

**Linux (crontab)**
```bash
# 每 30 分钟检查一次
*/30 * * * * cd /path/to/cjcx && python main.py -u 学号 -p 密码 -qq QQ号
```

**Windows (任务计划程序)**
1. 打开「任务计划程序」
2. 创建基本任务，设置触发器为每 30 分钟
3. 操作选择「启动程序」，填入 Python 和脚本路径

## 📝 通知示例

```
📢 成绩更新通知

📚 高等数学
   成绩: 0 | 学分: 0.0 | 绩点: 0.0
   学期: 2025-2026-1学期

📚 大学物理
   成绩: 0 | 学分: 0.0 | 绩点: 0.0
   学期: 2025-2026-1学期

📊 当前 GPA: 0.000
```

## ⚙️ 工作原理

1. **登录认证** - 使用 Playwright 模拟浏览器登录统一身份认证平台，OCR 自动识别验证码
2. **Cookies 管理** - 登录成功后缓存 Cookies，下次运行优先使用缓存
3. **成绩获取** - 调用教务系统 API 获取成绩列表
4. **增量检测** - 对比缓存的成绩记录，识别新增成绩
5. **消息推送** - 通过 OneBot HTTP API 发送 QQ 私聊通知

## 🔧 常见问题

### Q: 验证码识别失败怎么办？
A: 程序会自动刷新验证码重试（最多 10 次）。如果持续失败，请检查网络连接。

### Q: Cookies 失效后会怎样？
A: 程序会自动检测 Cookies 有效性，失效后自动重新登录。

### Q: 如何配置 OneBot？
A: 推荐使用 [Lagrange.OneBot](https://github.com/LagrangeDev/Lagrange.Core) 或 [NapCat](https://github.com/NapNeko/NapCatQQ)，配置 HTTP 服务后填入 `--webhook` 参数。

### Q: 支持多个账号吗？
A: 支持。每个账号的缓存文件独立存储，可以同时配置多个定时任务。

## ⚠️ 免责声明

- 本项目仅供学习交流使用
- 使用本项目所造成的一切后果由使用者自行承担
- 请勿用于任何商业或非法用途
- 请合理设置查询频率，避免对服务器造成压力

## 📄 License

[MIT License](LICENSE)