# SerialLens Web 智能体 · 部署指南

两种场景：**本机自用**（Windows，开机自动起）和**公网给评审看**（云服务器，带密码门禁）。

---

## 一、本机部署（Windows）

### 一条命令

```bash
pip install -e . fastapi "uvicorn[standard]"
python -m uvicorn webapp.app:app --host 127.0.0.1 --port 8300
```

或者双击仓库根目录的 `start_web.bat`（带 `--no-pause` 就不卡在按键等待）。

打开 `http://127.0.0.1:8300`。

### 开机自启

`start_web.bat` 的快捷方式放在启动文件夹：

```
%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup\SerialLens Web.lnk
```

指向 `start_web.bat`，参数 `--no-pause`，工作目录为仓库根目录。
重装或移动仓库后要重新指一遍，否则开机拉起的是旧路径。

### 访问密码

按优先级取，第一个非空生效：

1. 环境变量 `SERIALLENS_ACCESS_PASSWORD`
2. `webapp/.env.local`（本机密码文件，已 gitignore）
3. 都没有则自动生成，启动时在控制台打印一次

```bat
:: 临时固定密码
set SERIALLENS_ACCESS_PASSWORD=你的强密码
```

```bash
# 或者写进本地文件（推荐，重启不丢）
echo 你的强密码 > webapp/.env.local
```

强度要求：≥8 位且至少两类字符。弱密码只告警不拦截 —— 绑定 `0.0.0.0` 前自己确认。

### LLM

- 页面右上角 ⚙ 现场填，存 `webapp/.llm.local`（0600），重启不丢
- 或设环境变量 `SERIALLENS_API_KEY` / `DEEPSEEK_API_KEY` / `OPENAI_API_KEY`
- 不配也能跑：启发式、学习模式、代码生成全本地完成，只有「AI 解读」降级为模板

### 本机数据文件（都不进 git）

| 文件 | 内容 | 删了会怎样 |
| --- | --- | --- |
| `webapp/.env.local` | 访问密码 | 下次启动自动换新密码 |
| `webapp/.llm.local` | 页面里配的 LLM 配置（0600） | 需重新填 key |
| `webapp/sessions.db` | 会话历史 | 历史对话丢失 |
| `webapp/.profiles/*.json` | 网页里学的协议画像 | 画像丢失 |
| `~/.seriallens/profiles/*.json` | CLI 学的协议画像 | CLI 画像丢失 |

---

## 二、公网部署（给评审）

目标：一个 `http://<公网IP>:8300` 链接 + 访问密码，7×24 在线。

### 方案选型

| 方案 | 费用 | 适用 | 说明 |
| --- | --- | --- | --- |
| 阿里云 ECS 免费试用 | 0 元（新用户 3 个月） | **推荐** | 覆盖评审期；到期销毁实例 |
| 腾讯云/阿里云学生机 | 学生认证后 ≈ 0~10 元/月 | 长期 | 高校学生身份认证 |
| 海外免费 PaaS（Railway/Render 等） | 0 元 | ❌ 不要用 | 国内访问慢或不可达，评委打不开 |

域名与备案：**直接用 IP，不买域名**。国内服务器绑域名需 ICP 备案，周期数周。

### 路线一：ECS 裸机一键部署（约 15 分钟）

1. 阿里云控制台 → 免费试用 → 云服务器 ECS（Ubuntu 24.04，2核2G 起步）→ 设 root 密码，记下**公网 IP**
2. 安全组放行 `22`（SSH）和 `8300`（Web）
3. `ssh root@<公网IP>`
4. 服务器拉代码（GitHub 直连不通时用镜像）：
   ```bash
   git clone https://github.com/wdfdsyhb/SerialLens.git
   # 备用镜像：
   # git clone https://ghfast.top/https://github.com/wdfdsyhb/SerialLens.git
   ```
5. 一键部署：
   ```bash
   cd SerialLens && bash deploy/run.sh
   ```
   脚本做的事：建 `.venv` → 装依赖 → 问你要密码写进 `.env` → 生成 systemd 服务并 enable
6. 浏览器打开 `http://<公网IP>:8300`，输密码进入

服务管理：`systemctl status seriallens` / `systemctl restart seriallens`

> `run.sh` 里的默认密码是 `sgry..`（弱口令），**一定要改**。
> 正式评审前确认 `.env` 里是强密码，且 `EnvironmentFile` 指向正确。

### 路线二：Docker（服务器已装 Docker）

```bash
cd SerialLens
echo "SERIALLENS_ACCESS_PASSWORD=你的强密码" > .env
docker compose -f deploy/docker-compose.yml up -d --build
```

镜像基于 `python:3.12-slim`，只拷 `pyproject.toml` / `README.md` / `src` / `webapp`，
不含测试和本机数据，`EXPOSE 8300`。

---

## 三、评审期检查清单

- [ ] 安全组 8300 对 `0.0.0.0/0` 开放（密码门禁挡住匿名访问）
- [ ] `.env` 是强密码，不是 `sgry..`
- [ ] `systemctl is-active seriallens` 为 active，`restart=always` 生效
- [ ] `curl http://127.0.0.1:8300/api/health` 返回 200
- [ ] 用手机流量（非本机网络）访问一次，确认公网可达
- [ ] 报名系统提交：链接 `http://IP:8300` + 访问密码 + 测试说明
- [ ] 试用期到期日在总决赛之后，到期前如需续走学生机路线
- [ ] 重启服务器一次，确认服务自动拉起、会话历史还在

---

## 四、排障

| 现象 | 原因 | 处理 |
| --- | --- | --- |
| 端口被占 | 上次进程没退 | `netstat -ano \| findstr :8300` → `taskkill /PID <pid> /F` |
| 开机没起来 | 快捷方式还指向旧路径 | 检查启动文件夹 `.lnk` 的 TargetPath 和 WorkDir |
| 页面能开但对话 500 | 缺 `fastapi` / `uvicorn` | `pip install fastapi "uvicorn[standard]"` |
| `ModuleNotFoundError: webapp` | 不在仓库根目录 | `cd` 到仓库根再起，`webapp` 靠 cwd 解析 |
| 端口通但页面空白 | `static/` 没一起拷 | Docker 场景确认 `COPY webapp ./webapp` |
| 忘密码 | 不知道当前生效哪个 | 看启动日志首行打印；或删 `webapp/.env.local` 重启换新密码 |
