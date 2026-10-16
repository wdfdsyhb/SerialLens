# SerialLens Web 智能体 · 部署指南

目标：给评审专家一个 7×24 在线可访问的智能体链接（如 `http://<公网IP>:8300`），带访问密码门禁。

## 方案选型（免费优先）

| 方案 | 费用 | 适用 | 说明 |
|---|---|---|---|
| 阿里云 ECS 免费试用 | 0 元（新用户 3 个月） | **推荐** | 覆盖校赛到总决赛评审期；到期销毁实例即可 |
| 腾讯云/阿里云学生机 | 学生认证后 ≈ 0~10 元/月 | 长期 | 高校学生身份认证 |
| 海外免费 PaaS（Railway/Render 等） | 0 元 | ❌ 不要用 | 国内访问慢或不可达，评委打不开=作品不合格 |

域名与备案：**直接用 IP 访问，不买域名**（国内服务器绑域名需 ICP 备案，周期数周，比赛来不及）。

## 路线一：ECS 裸机一键部署（推荐，约 15 分钟）

1. 阿里云控制台 → 免费试用 → 云服务器 ECS（选 Ubuntu 24.04，2核2G 起步）→ 设置 root 密码，记下**公网 IP**
2. 安全组：放行 `22`（SSH）和 `8300`（Web）端口
3. 本地连接：`ssh root@<公网IP>`
4. 服务器上拉代码（GitHub 直连不通时用镜像）：
   ```bash
   git clone https://github.com/wdfdsyhb/SerialLens.git
   # 备用镜像：
   # git clone https://ghfast.top/https://github.com/wdfdsyhb/SerialLens.git
   ```
5. 一键部署：
   ```bash
   cd SerialLens && bash deploy/run.sh
   ```
   （脚本完成：装依赖 → 写密码 .env → systemd 常驻服务）
6. 浏览器打开 `http://<公网IP>:8300`，输入访问密码进入

服务管理：`systemctl status/restart seriallens`

## 路线二：Docker 部署（服务器已装 Docker 时）

```bash
cd SerialLens
echo "SERIALLENS_ACCESS_PASSWORD=你的密码" > .env
docker compose -f deploy/docker-compose.yml up -d --build
```

## LLM API Key（可选，增强评委体验）

- 方式一：部署后打开页面右上角 ⚙，现场填 key（存服务内存，重启失效）
- 方式二：`deploy/run.sh` 生成的 `.env` 里追加一行 `SERIALLENS_API_KEY=sk-...` 后 `systemctl restart seriallens`（重启不丢，推荐评审期用这种）

## 评审期检查清单

- [ ] 安全组 8300 对 0.0.0.0/0 开放（密码门禁已挡住匿名访问）
- [ ] `systemctl is-active seriallens` 为 active；`restart=always` 已生效
- [ ] 用手机流量（非本机网络）访问一次确认公网可达
- [ ] 报名系统里提交：链接 `http://IP:8300` + 访问密码 + 测试说明
- [ ] 试用期到期日（控制台可见）在总决赛之后；到期前如需续，再走学生机路线
