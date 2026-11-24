# pyapollo-config-client

一个独立的、可发布的 Python Apollo（携程配置中心）客户端：

- 支持 **Properties / YAML / JSON** 命名空间；
- 支持启动拉取（pull）、`/notifications/v2` 长轮询或**定时轮询**热更新；
- 支持 **Namespace 自动发现**与 **Meta Server 服务发现**（定期刷新 Config Service 地址）；
- 提供结构化变更事件、按 namespace / key 过滤的监听器；
- 提供**心跳补偿拉取**，修复进程重启或网络抖动期间漏掉的通知；
- 支持**运行时更新客户端参数**（`update_options`），无需重建客户端；
- 提供**原生 asyncio 客户端** `AsyncApolloClient`；
- 支持磁盘缓存容灾、Apollo `secret` 签名、多 Config Service 故障转移；
- 附带 Mock Apollo、FastAPI Demo 与完整测试。

> 发布名：`pyapollo-config-client`，导入名：`apollo_client`。
> 参考了 [filamoon/pyapollo](https://github.com/filamoon/pyapollo)、
> [BruceWW/pyapollo](https://github.com/BruceWW/pyapollo)、
> [xhrg-product/apollo-client-python](https://github.com/xhrg-product/apollo-client-python)、
> [OuterCloud/pyapollo](https://github.com/OuterCloud/pyapollo)
> 的公开实现思路，但代码为独立实现，不依赖上述项目。

## 安装

```bash
pip install pyapollo-config-client
```

需要原生异步客户端（httpx）时：

```bash
pip install "pyapollo-config-client[async]"
```

需要 Demo 依赖（FastAPI / Uvicorn）时：

```bash
pip install "pyapollo-config-client[demo]"
```

要求 Python >= 3.10，核心依赖只有 `requests` 与 `PyYAML`。

## 快速开始

```python
from apollo_client import ApolloClient

client = ApolloClient(
    app_id="demo-app",
    config_server_url="http://127.0.0.1:18080",
    cluster="default",
    namespaces=["application", "demo.yaml"],
)

client.start()  # 首次拉取 + 启动长轮询监听

# properties 值保持字符串
print(client.get_value("app.name"))

# YAML 支持点号路径，返回原生类型
print(client.get_value("server.port", namespace="demo.yaml"))
print(client.get_int("server.port", namespace="demo.yaml"))

# 类型化读取
print(client.get_bool("app.feature.new_ui"))
print(client.get_json("database.pool", namespace="demo.yaml"))

# 客户端状态（watch mode、节点、最近拉取 / 心跳）
print(client.status())

client.stop()
```

格式识别规则：

| namespace | 格式 |
| --- | --- |
| `application` | properties（Apollo 默认） |
| `application.properties` | properties |
| `demo.yaml` / `demo.yml` | YAML |
| `feature.json` | JSON |

没有后缀的 namespace 默认按 properties 处理；需要时显式指定：

```python
client = ApolloClient(
    app_id="demo-app",
    namespaces=["feature"],
    namespace_formats={"feature": "yaml"},
)
```

## 热更新模式

```python
# 默认：长轮询（秒级热更新，服务端无变更时挂起连接）
client = ApolloClient(..., watch_mode="long_poll", long_poll_timeout=60.0)

# 可选：定时轮询（实现简单，适合不支持 /notifications/v2 的代理或老版本）
client = ApolloClient(..., watch_mode="poll", poll_interval=30.0)
```

两种模式都使用同一套变更事件与监听器；轮询失败按 1s → 2s → … → 30s 退避。运行中可通过 `update_options(watch_mode=...)` 切换，无需重启客户端。

## 热更新监听

```python
from apollo_client import ChangeEvent


def on_change(event: ChangeEvent) -> None:
    print(event.namespace, event.changed_keys)
    for key, change in event.changes.items():
        print(change.kind, key, change.old, "->", change.new)


# 监听所有 namespace
client.add_listener(on_change)

# 只监听 demo.yaml 中的 server.port / server.host
client.add_listener(
    on_change,
    namespace="demo.yaml",
    keys=["server.port", "server.host"],
)

# 装饰器写法
@client.on_change(namespace="application", keys=["app.limits.max"])
def on_limit_changed(event: ChangeEvent) -> None:
    print("new limit:", event.changes["app.limits.max"].new)
```

监听器在客户端后台线程（异步客户端中为 watcher task）执行，请保持轻量；异常会被记录，不会中断监听。

## Namespace 自动发现

```python
# 启动时自动发现该 app/cluster 下所有 namespace
client = ApolloClient(
    app_id="demo-app",
    config_server_url="http://apollo-config-service:8080",
    namespaces=["application"],   # 作为兜底，发现成功后一并监听发现结果
    auto_discover=True,
)

# 也可以显式触发（例如新上线一个 namespace 后）
names = client.discover_namespaces()
print(names)  # ('application', 'demo.yaml', 'feature.json', ...)
```

发现接口为 `/apps/{appId}/clusters/{cluster}/namespaces`。部分新版本 Apollo 可能未开放该接口，此时客户端只记录 warning 并保留显式 namespace 列表，不影响启动。

## Meta Server 服务发现

```python
client = ApolloClient(
    app_id="demo-app",
    config_server_url="http://apollo-config-service:8080",  # 首次发现前的兜底地址
    meta_server_url="http://apollo-meta:8080",
    meta_refresh_interval=300.0,  # 每 5 分钟重新解析一次 Config Service 地址
    namespaces=["application", "demo.yaml"],
)
```

- 请求 `GET {meta_server_url}/services/config?appId=...&ip=...`，支持返回 `["host:port", ...]`、`{"configServers": [...]}`、`{"homepageUrl": "..."}` 等常见形状；
- 多个 Meta Server 地址可逗号分隔，逐个尝试；
- 解析失败时保留当前 Config Service 地址并记录 warning，不中断热更新；
- 运行中修改 Meta Server：`client.update_options(meta_server_url=...)`，会立即刷新一次。

## 心跳与补偿拉取

Apollo 的 `/notifications/v2` 长轮询在客户端重启、网络切换或服务端通知丢失时可能漏掉一次变更。客户端默认每 **600 秒**做一次全量校验（与 xhrg 客户端的心跳行为一致）：

```python
client = ApolloClient(..., heartbeat_interval=600.0)  # 默认；设为 None / 0 关闭
```

- 心跳会逐个 namespace 拉取 `/configs`，发现 releaseKey / 数据变化时走与长轮询相同的 diff 与回调流程；
- `client.status()` 暴露 `heartbeatCount`、`lastHeartbeatAt`、`heartbeatOk`、`heartbeatError`，便于接入健康检查；
- 心跳与长轮询、Meta Server 刷新在独立后台线程中执行，互不阻塞。

## 运行时更新客户端参数

```python
client.update_options(
    config_server_url="http://apollo-config-service-2:8080",
    secret="new-secret",
    timeout=5.0,
    long_poll_timeout=60.0,
    ip="10.0.0.8",                 # 灰度发布 IP
    watch_mode="poll",
    poll_interval=15.0,
    heartbeat_interval=300.0,
)
```

- 支持 `app_id`、`cluster`、`config_server_url`、`meta_server_url`、`secret`、`timeout`、`long_poll_timeout`、`ip`、`watch_mode`、`poll_interval`、`heartbeat_interval`、`meta_refresh_interval`、`namespaces`、`headers`；
- 修改 `app_id` / `cluster` 会清空旧应用的内存快照并切换磁盘缓存目录（不会删除旧缓存文件）；
- 修改 `namespaces` 为增量注册；同步客户端会在下次读取、轮询或长轮询时拉取新增 namespace，异步客户端的 `update_options` 会立即拉取；
- 未知参数会抛 `TypeError`，非法取值抛 `ValueError`。

## 手动刷新与关闭

```python
# 手动拉取某个 namespace 或全部已注册 namespace；返回每个 namespace 的变更事件
events = client.refresh("application")
client.refresh()

client.stop()
```

客户端也支持上下文管理器：

```python
with ApolloClient(
    app_id="demo-app",
    config_server_url="http://127.0.0.1:18080",
    namespaces=["application"],
) as client:
    print(client.get_value("app.name"))
```

## 原生异步客户端

```bash
pip install "pyapollo-config-client[async]"
```

```python
import asyncio

from apollo_client import AsyncApolloClient


async def main() -> None:
    async with AsyncApolloClient(
        app_id="demo-app",
        config_server_url="http://apollo-config-service:8080",
        namespaces=["application", "demo.yaml"],
        meta_server_url="http://apollo-meta:8080",
    ) as client:
        print(client.get_value("app.name"))

        async def on_change(event):
            print("changed:", event.changed_keys)

        client.add_listener(on_change, namespace="demo.yaml")
        await asyncio.sleep(60)


asyncio.run(main())
```

- HTTP 使用 `httpx.AsyncClient`，不占用线程池；
- 读取（`get_value` / `get_int` / `get_json` …）是内存操作，保持同步；
- 首次拉取 / 缓存回填使用 `await client.aget_config(namespace)` 或 `await client.refresh()`；
- 监听器可以是普通函数或 `async def`，协程会被等待；
- 支持长轮询、定时轮询、Namespace 发现、Meta Server、心跳、磁盘缓存、`update_options` 与 `status()`。

## FastAPI 生命周期集成（同步客户端）

```python
from contextlib import asynccontextmanager
from functools import partial

from fastapi import FastAPI
from starlette.concurrency import run_in_threadpool

from apollo_client import ApolloClient

client = ApolloClient.from_env(
    app_id="demo-app",
    config_server_url="http://apollo-config-service:8080",
    namespaces=["application", "demo.yaml"],
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    await run_in_threadpool(client.start)
    try:
        yield
    finally:
        await run_in_threadpool(partial(client.stop, timeout=2.0))


app = FastAPI(lifespan=lifespan)


@app.get("/config/{key:path}")
def read_config(key: str):
    return {"key": key, "value": client.get_value(key)}
```

配置读取是内存字典操作，可以直接在请求处理函数中调用；`start()` / `stop()` 属于阻塞操作，放进线程池即可（或直接使用异步客户端）。

## 环境变量

`ApolloClient.from_env()` / `AsyncApolloClient.from_env()` 支持以下变量（环境变量优先于默认参数）：

| 变量 | 说明 |
| --- | --- |
| `APOLLO_APP_ID` | 应用 ID |
| `APOLLO_CONFIG_SERVER_URL` / `APOLLO_CONFIG_SERVER` | Config Service 地址，可逗号分隔多个 |
| `APOLLO_CLUSTER` | 集群，默认 `default` |
| `APOLLO_NAMESPACES` | 逗号分隔，如 `application,demo.yaml` |
| `APOLLO_SECRET` | Apollo 访问密钥 |
| `APOLLO_TIMEOUT` | 普通请求超时（秒） |
| `APOLLO_LONG_POLL_TIMEOUT` | 一次长轮询的服务端等待时间（秒），应不小于 Apollo 服务端配置（默认 60） |
| `APOLLO_WATCH_MODE` | `long_poll`（默认）或 `poll` |
| `APOLLO_POLL_INTERVAL` | 定时轮询间隔（秒，默认 60） |
| `APOLLO_HEARTBEAT_INTERVAL` | 心跳 / 补偿拉取间隔（秒，默认 600；`0` 关闭） |
| `APOLLO_META_SERVER` / `APOLLO_META_SERVER_URL` | Meta Server 地址，可逗号分隔多个 |
| `APOLLO_META_REFRESH_INTERVAL` | Meta Server 刷新间隔（秒，默认 300） |
| `APOLLO_AUTO_DISCOVER` | 是否在启动时自动发现 namespace |
| `APOLLO_IP` | 灰度发布使用的客户端 IP |
| `APOLLO_CACHE_DIR` | 缓存根目录 |
| `APOLLO_CACHE_ENABLED` | 是否启用磁盘缓存 |
| `APOLLO_PRELOAD_CACHE` | 是否在构造时预加载缓存 |

## 本地缓存与容灾

- 每个 namespace 保存为 `{cache_dir}/{app_id}/{cluster}/{namespace}.json`，原子写入，权限 `0600`。
- 默认在构造时预加载缓存，因此 Config Service 暂时不可用时，进程重启后仍能读到上次快照。
- `client.start()` 拉取失败只记录 warning，不会丢弃缓存，也不会阻塞服务启动。
- 缓存内容包含 `releaseKey`、`notificationId`、原始文本与解析后的数据。
- 可通过 `client.get_config(ns).source` 判断当前快照来自 `remote` 还是 `cache`。

## 包结构

```text
pyapollo-config-client/
├── pyproject.toml
├── README.md
├── COMPARISON.md
├── LICENSE
├── CHANGELOG.md
├── src/apollo_client/       # 发布到 wheel 的客户端代码
│   ├── client.py            # ApolloClient（同步）
│   ├── async_client.py      # AsyncApolloClient / AsyncApolloTransport（可选 httpx）
│   ├── transport.py         # HTTP、签名、多节点故障转移、长轮询、namespace 发现
│   ├── meta.py              # Meta Server /services/config 解析与刷新
│   ├── env.py               # APOLLO_* 环境变量
│   ├── config.py            # NamespaceConfig 快照、格式识别、diff
│   ├── parser.py            # properties / YAML / JSON 解析
│   ├── cache.py             # 磁盘缓存
│   └── errors.py
├── mock_apollo/             # 本地 Mock Apollo（源码示例，不进入 wheel）
├── demo/                    # FastAPI Demo + Dashboard（源码示例，不进入 wheel）
└── tests/                   # pytest 测试
```

wheel 只包含 `apollo_client`；`demo/` 和 `mock_apollo/` 位于源码包 `sdist` 中，方便直接在仓库/源码包中运行。

## 开发与测试

```bash
cd packages/apollo-client
uv sync --group dev
uv run pytest -q
```

也可以使用 pip：

```bash
python -m venv .venv
. .venv/bin/activate
pip install -e ".[demo,test]"
pytest -q
```

测试覆盖（共 68 个）：

- properties 注释、分隔符、续行、转义、Unicode、重复键；
- YAML/JSON 嵌套、类型、空文档、非法格式；
- 点号路径、类型化取值、diff；
- 真实 HTTP 拉取、properties/YAML 热更新、监听器过滤；
- 手动刷新、动态 namespace、多 Config Service 故障转移；
- `secret` 签名头、磁盘缓存离线回退；
- Namespace 自动发现、定时轮询、Meta Server 发现、心跳补偿、运行时参数更新；
- 同步 / 异步客户端的拉取、热更新、缓存、发现与状态；
- FastAPI 各接口、Dashboard、SSE 热更新事件流。

## 运行 Demo

在源码目录中先启动 Mock Apollo：

```bash
cd packages/apollo-client
uv run python -m mock_apollo
# http://127.0.0.1:18080
# Swagger: http://127.0.0.1:18080/docs
```

再启动 Demo：

```bash
uv run uvicorn demo.main:app --port 8000
```

打开 <http://127.0.0.1:8000>，可以看到：

- 左侧实时展示 `application`（properties）与 `demo.yaml`（YAML）的解析结果、release key、来源与原始内容；
- 右侧通过 SSE 展示热更新事件（key、旧值、新值）；
- 右上角状态表示后台监听连接是否存活，`/health` 额外返回 watch mode、最近拉取 / 心跳时间。

API：

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `GET` | `/health` | 客户端状态、watch mode、最近拉取 / 心跳、namespace、release key |
| `GET` | `/config` | 所有 namespace 的快照（含原始内容） |
| `GET` | `/config/{namespace}` | 单个 namespace 快照 |
| `GET` | `/config/{namespace}/{key}` | 单个配置值 |
| `POST` | `/config/refresh?namespace=...` | 手动强制拉取 |
| `GET` | `/events` | SSE 热更新事件流 |

演示热更新：

```bash
curl -X PUT http://127.0.0.1:18080/admin/configs/demo-app/default/application \
  -H 'Content-Type: application/json' \
  -d '{
    "content": "app.name=Hot Reloaded\napp.version=0.2.0\napp.feature.new_ui=false\napp.limits.max=200\nlogging.level=DEBUG"
  }'
```

提交后 1～2 秒内，Dashboard 右侧会出现变更事件，左侧配置自动刷新。也可以直接观察 SSE：

```bash
curl -N http://127.0.0.1:8000/events
```

## 构建与发布

### 打 Tag 自动发布（推荐）

仓库包含 [.github/workflows/publish.yml](.github/workflows/publish.yml)：推送 `v*` tag 时会自动运行测试、校验 tag 与版本号、构建 wheel / sdist 并发布到 PyPI。

1. 在 PyPI 创建 API token，并在 GitHub 仓库 **Settings → Secrets and variables → Actions** 中添加名为 `PYPI_API_TOKEN` 的 secret。
   - 首次发布（项目尚不存在）可使用账号级 token；项目创建后可改为项目级 token。
   - 也可以改用 **Trusted Publisher**（OIDC）：删除 workflow 中的 `password`，在 PyPI 配置 `publish.yml` + 环境 `pypi`，并保留 `id-token: write`。
2. 更新版本号（见下文），提交并推送。
3. 创建并推送 tag（tag 必须为 `v` + 版本号）：

```bash
git tag v0.2.0
git push origin v0.2.0
```

工作流会：

- 在 Python 3.12 上安装依赖并运行全部 68 个测试；
- 校验 tag 与 `pyproject.toml` 的 `[project].version` 一致，不一致直接失败；
- 构建 `dist/`，并使用 `pypa/gh-action-pypi-publish` 以 `PYPI_API_TOKEN` 发布到 PyPI；
- 支持改用 OIDC Trusted Publishing：删除 `password` 并配置 PyPI 的 Trusted Publisher 即可，无需保存 token。

> 如果发布失败，请先确认 secret 名称是否为 `PYPI_API_TOKEN`、token 是否已过期，以及 PyPI 上是否已存在同版本文件。

### 手动构建（本地检查）

```bash
uv sync --group dev
uv run pytest -q
uv build
uvx twine check dist/*
```

`dist/` 是构建产物，已在 `.gitignore` 中忽略，不提交到仓库。

也可以在发布前用干净虚拟环境验证 wheel：

```bash
python -m venv /tmp/apollo-wheel-test
/tmp/apollo-wheel-test/bin/pip install dist/pyapollo_config_client-0.2.0-py3-none-any.whl
/tmp/apollo-wheel-test/bin/python -c "from apollo_client import ApolloClient, __version__; print(__version__)"
```

版本号同时维护在：

- `pyproject.toml` 的 `[project].version`
- `src/apollo_client/_version.py`

发布前请确认两者一致，并更新 `CHANGELOG.md`；打 tag 前请先在本地跑通测试。

## 协议说明

客户端使用 Apollo Config Service 的标准接口：

```text
GET /configs/{appId}/{cluster}/{namespace}
GET /configfiles/{appId}/{cluster}/{namespace}?format=yaml
GET /notifications/v2?appId=...&cluster=...&notifications=[...]
GET /apps/{appId}/clusters/{cluster}/namespaces        # namespace 发现
GET {metaServer}/services/config?appId=...&ip=...      # Meta Server
```

- `properties`：`configurations` 直接是 key/value map。
- `yaml` / `json`：`configurations` 通常是 `{namespace: 完整文本}`；客户端解析该文本，并在形状异常时回退到 `/configfiles`。
- 长轮询：服务端无变更返回 `304`；有变更返回 `[{namespaceName, notificationId}]`，客户端重新拉取对应 namespace。
- 定时轮询：按 `poll_interval` 逐个拉取 namespace，失败指数退避。
- 收到 `304` 时继续下一次长轮询；网络异常按 1s → 2s → … → 30s 退避。
- 心跳：按 `heartbeat_interval` 全量拉取，用于补偿漏通知；与长轮询共用 diff / 回调逻辑。
- Meta Server 返回的 Config Service 列表会在 `meta_refresh_interval` 后重新解析并替换传输层地址。

## 同类客户端能力对比

与 Apollo 生态中常见的四个 Python 客户端（filamoon、BruceWW、xhrg、OuterCloud）的完整能力矩阵、维护状态和选型建议见 [COMPARISON.md](COMPARISON.md)。

简要结论：

- 只需要最简 properties 拉取：filamoon / BruceWW，但两者都已多年未更新；
- 需要单进程内快速接入、YAML / JSON、秒级热更新、服务发现、异步与完整可观测：本包 `pyapollo-config-client`；
- 本包在 0.2.0 起覆盖了此前缺失的 Namespace 自动发现、定时轮询、Meta Server、心跳、运行时参数更新和原生异步，不再需要组合多个客户端。

## 与参考实现的差异

- 参考实现主要面向 properties，YAML 命名空间的内容不会被解析成嵌套结构。
- 本实现将配置保存为只读快照，读取路径不做锁和网络 I/O，适合 Web 请求线程直接访问。
- 变更事件包含结构化 diff，可直接驱动缓存刷新、指标上报或 SSE 推送。
- 同时提供同步与原生异步客户端，共享签名、解析、缓存和事件语义。
- 增加格式识别、磁盘缓存、认证签名、多节点故障转移、服务发现、心跳和完整测试。

## License

Apache-2.0，见 [LICENSE](LICENSE)。
