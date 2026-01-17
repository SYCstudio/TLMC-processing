# TLMC 分轨自动化脚本

Author: [sycstudio](https://github.com/sycstudio) with AI assistance.

## 功能

主要功能：根据 .cue 文件提供的信息，对音频文件进行分轨。  
同时执行一些额外转码操作，比如将 ".wav", ".wv", ".ape", ".tta", ".alac" 转码为 flac。  
支持增量化地从 TLMC 远端拉取新的专辑并处理。

## 依赖

Python 版本： `3.13` 及以上版本。  
要求 ffmpeg 可用（能在命令行直接调用）。  
用到的 python 库：rich, cuetools。这两个库都可以通过 pip 直接安装。

## 使用方法

### 首次执行

将目录准备为如下形式：

```plain
- incoming          # 即 TLMC 拉取下来的位置，保持其原本结构即可
    - artist1
        - album1
        - album2
    - artist2
        - album3
            - disc1
            - disc2
tlmc-processing.py
```

执行 `python tlmc-processing.py` 即可。

程序执行完成后会在目录下生成以下文件和目录：

* `library` ：转码后的音频文件所在目录，保持原本在 TLMC 中的结构。  
* `error` ：在处理过程中发生错误的专辑，需要手工处理。也同样保持原本在 TLMC 中的结构。  
* `log` ：日志归档，将之前执行过程中的日志归档到该文件夹下。  
* `.processed` ：记录已经处理过的专辑，以文件夹的形式，一行一个。该文件用于 `rsync` 增量同步时排除已经下载的专辑。
* `.error_processed` ：记录在处理过程中发生错误的专辑，以文件夹的形式，一行一个，对应在本次处理中，error 中的文件夹。当手动处理完成后，需要将该文件中的记录手动添加到 `.processed` 文件中。  
* `processing.log` ：处理过程中的日志。
* `error.log` ：处理过程中发生错误的日志。
* `moved_additional_files.log` ：处理过程中移动的额外文件的日志。该文件会记录那些没有被判定为包含专辑但拥有其他文件的文件夹，比如一些图片文件。如果你对是否遗漏了某些文件夹有疑问，可以参考该文件。

### 增量从 TLMC 远端拉取新的专辑并处理

按照以下步骤执行

1. 执行 rsync 增量拉取：`rsync-ssl -azhP -vv --exclude-from=.processed 'rsync://patchouli@rsync.thdisc.com:874/tlmc/' 'incoming'`。该步骤会读取 `.processed` 文件，排除已经处理过的专辑，然后从 TLMC 远端拉取新的专辑到 `incoming` 目录下。  
2. 执行本地转换：`python tlmc-processing.py`。该步骤会读取 `incoming` 目录下的专辑，并进行转换。  
3. 查看 `error.log` 以及 `error` 文件夹，手动处理发生错误的专辑。注意，如果你手动处理了某个专辑，需要在 `.error_processed` 找到对应的行，并手动添加到 `.processed` 文件中。  

## 流水线

以下是本处理脚本的基本流水线

```plain
1. 在 incoming 目录下，收集所有待处理的专辑。一个文件夹被认为是一个专辑，当且仅当其包含至少一个以下扩展名的文件：[".cue", ".flac", ".wav", ".mp3", ".ogg", ".ape", ".aac", ".wv"]。如果当前文件夹被判定为专辑，则不继续递归子文件夹。

for each album, enter its directory:
    try:
        if 如果当前目录下存在 .cue 文件
            收集每一个 cue 文件并解析
                首先尝试使用 cuetools 解析；如果解析失败则手工解析
                如果该 cue 文件包含多个音频文件
                    如果这些音频文件都已经存在
                        跳过当前 cue，因为其已经处理过了
                    否则
                        多文件的 cue 处理过于复杂，标记为异常，需要手工处理
                否则
                    查找目录下与该 cue 中描述的音频文件最相似（计算字符串距离）的文件，记录匹配
            对每个匹配的音频文件，选择一个最相似的 cue 文件
            如果上述匹配结束后，发现当前目录下有多个 cue-music 匹配对
                为每一个匹配对创建单独的子文件夹，并在子文件夹中做切分
            否则
                直接在当前目录下切分
            删除原始音频结果
        elif 否则，如果当前目录下存在 .iso 文件
            理论上这里可以使用 `sacd_extract` 工具将 iso 文件提取为分轨的 dsf 文件。然后再进行转码。
            但考虑到 TLMC 目录下这样的 SACD 专辑少于 10 个，所以将其标记为异常，需要手工处理
        elif 否则，收集当前目录下所有扩展名为 [".wav", ".mp3", ".ogg", ".ape", ".aac", ".wv"] 的文件，将其转码为 flac。
    except Exception as e:
        将专辑移动到 error 目录下，标记为异常，需要手工处理。
    将专辑移动到 library 目录下，标记为已处理。

收集额外的非空目录中的文件，将其移动到 library 对应目录下。
```

## 执行结果分析

在 2026.1.17 首次执行，大概扫描到 17000-18000 个专辑。其中需要手工处理的约 10+ 个。  
使用 epyc 7551p 2.0Ghz CPU 且数据存放在 HDD 上，大约需要 15 个小时处理完。  
