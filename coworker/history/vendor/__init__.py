"""Vendored third-party packages used by DeskMate 史料检索.

每个子目录都是一个**原样拷贝**的第三方发行版（代码与数据均未修改），各自携带
自己的 LICENSE。修改前请先确认上游许可条款。

- ``opencc/`` — opencc-python-reimplemented 0.1.7（OpenCC 的纯 Python 实现）
  - 代码与字典均为 **Apache License 2.0**（上游 OpenCC/BYVoid 同样是 Apache-2.0），
    可安全随 MIT 项目分发；NOTICE.txt 已随包保留，满足 Apache-2.0 第 4 条的署名要求。
  - 为什么不用 zhconv：zhconv 代码是 MIT，但其字典 ``zhcdict.json`` 衍生自
    MediaWiki 而按 **GPLv2** 授权，具有传染性，不能进 MIT 仓库。
"""

__all__: list[str] = []
