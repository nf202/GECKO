# GECKO

[GitHub 仓库](https://github.com/nf202/GECKO)

GECKO 通过图结构与查询特征的协同演化测试图数据库逻辑错误，包含边界值挖掘、
封闭 AST 查询生成、独立参考解释器和解释感知反馈。

本目录对应论文 *Co-Evolving Property Graphs and Query Features for Logic Bug
Detection in Graph Database Systems* 的定稿实现，使用身份修复后的 transport2
运行入口。保留实验代码的目录与模块名，便于核对代码快照。

## 离线使用

Python 3.11 及以上，在本目录执行：

```sh
python -m venv .venv
# Linux/macOS: source .venv/bin/activate
# Windows PowerShell: .venv\Scripts\Activate.ps1
python -m pip install -e ".[test]"
python scripts/check_reference.py
python -m pytest
python scripts/doctor.py
python tools/verify_manifest.py
```

离线示例会对小图上的特征进行准入检查，生成 Cypher/Gremlin 并计算参考结果，
无需数据库、模型或 API 密钥。doctor 只检查运行条件，不启动数据库；源码包中
缺少数据库二进制是正常情况。

## 运行实验

数据库实验需要 Linux、项目内的数据库运行环境、Java 和模型工作器。
先阅读[环境说明](docs/RUNTIME.md)及[复现说明](docs/REPRODUCING.md)。

```sh
python -m pip install -e ".[engines]"
python scripts/gecko_transport2_entry_20261109.py --help
```

正式入口会安装实验使用的精确序列化适配器。输出目录必须位于当前项目的
`runs/` 下且尚不存在。实验使用独立测试实例，并会重置这些实例内的图。
配置文件只含单 seed 轨迹，不会自动安排多 seed 实验。

`scripts/` 为实验实现，`src/gqltest/` 为共享语义与引擎支持，`tests/` 为离线测试，
`examples/` 为示例，`configs/` 为实验参数，`docs/` 为架构、复现及来源记录。
运行完整实验需要完整源码目录及 editable 安装，单独安装 wheel 不包含完整实验入口。

本次整理未打包数据库二进制、模型凭据、原始运行日志、历史探索脚本或论文草稿。
实验代码仅调整环境绑定部分，原始与发布版哈希见
[来源清单](docs/source-provenance.json)。

公开前仍需作者确定开源许可证并补齐引用信息；当前未附加许可证。
实验结果及缺陷认定材料单独维护，详见[发布说明](docs/PUBLICATION.md)。
