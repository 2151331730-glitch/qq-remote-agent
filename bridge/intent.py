# -*- coding: utf-8 -*-
"""闲聊 / 任务分流。

为什么要分流：把每条 QQ 消息都包装成「远程任务，请直接执行并汇报」，闲聊也会被
当成工单处理，语气像运维报告。分流后两条通道各自用不同的提示词和会话：

- task：要在电脑上动手（改文件、跑命令、查东西）→ 执行语义 + 任务会话
- chat：只是聊天/提问 → 手机 App 那种自然对话 + 闲聊会话

规则优先（零延迟、零成本），不确定时默认闲聊——闲聊通道里模型照样能操作本机，
判错的代价只是语气不同；反过来默认任务会让每句"在吗"都变成工单。
用户可以用 `/执行 <内容>` / `/聊 <内容>` 强制指定。
"""
import re

# 明确要动手的动词 / 短语
_VERB = re.compile(
    r"帮我|帮忙|替我|给我(发|找|查|写|改|弄|看|下载|截图)|"
    r"改一下|改下|改成|修改|修复|编辑|跑一下|跑个|跑一遍|运行|执行|打开|关掉|关闭|"
    r"启动|重启|安装|卸载|下载|上传|部署|编译|调试|整理|备份|删除|删掉|移动|复制|"
    r"重命名|推送|提交|截图|录屏|生成|创建|新建|写个|写一个|写一下|保存|读取|读一下|"
    r"分析|对比|检查|测试一下|测试下|查找|搜索|搜一下|查一下|查查|找一下|列一下|列出|"
    r"发我|发给我|发一下|定时|提醒我|记一下|清空|压缩|解压|转换|导出|导入|统计")

# Windows / WSL 路径，出现基本就是要看某个实际位置
_PATH = re.compile(r"[A-Za-z]:[\\/]|~[/\\]|/mnt/[a-z]|\./[\w一-鿿]")

# 疑问句：问"是什么/为什么"时，哪怕提到文件也按闲聊处理（模型照样能答）
_QUESTION = re.compile(
    r"[?？]|吗|呢|为什么|是什么|什么意思|怎么(用|办|样|回事)|如何|能不能|可不可以|"
    r"是不是|要不要|哪个|哪些|你觉得")

# 「问概念」而不是「问状态」：这类疑问是在问"什么意思/为什么/怎么办"，
# 不需要看电脑，凭知识就能答。判在 _NOUN 之前，避免「这个报错是什么意思」
# 因为提到"报错"就被当成要去查日志。
_CONCEPT_Q = re.compile(
    r"是什么(意思|原理|东西)|什么意思|为什么(会|要|是)|啥意思|"
    r"怎么(用|理解|办)|如何(理解|使用)|原理|区别|哪个好|该学|建议")

# 电脑上的东西，提到就大概率是任务
_NOUN = re.compile(
    r"文件|文件夹|目录|路径|桌面|代码|项目|仓库|程序|进程|端口|命令|脚本|日志|配置|"
    r"数据库|报错|环境变量|\bgit\b|\bgithub\b|\bnpm\b|\bpip\b|\bpython\b|powershell|\bcmd\b|"
    r"\bdocker\b|\bwsl\b|\bapi\b|\bpytest\b|\bcommit\b|\bpush\b", re.I)

# 英文命令打头（"git status" / "npm run dev"），排除了纯提问的情况
_EN_CMD = re.compile(
    r"^\s*(run|fix|edit|open|check|find|install|deploy|build|test|git|npm|pip|"
    r"python|cd|ls|dir|cat|taskkill|curl)\b", re.I)

LONG_TEXT = 160


def classify(text: str, has_images: bool = False):
    """返回 (kind, reason)；kind 为 "chat" 或 "task"。

    **顺序很重要**：疑问句要先判，再判任务动词。

    真实踩过的坑：「收到开始执行这句话能不能口语化」——
    它是在讨论说话风格，但"执行"命中了任务动词，被当成工单跑了 4 分钟。
    用户看到的是"我问个问题，它闷头干活"。

    例外：问句里明确点到电脑上的东西（文件/路径/进程/仓库…），仍然算任务，
    因为"这个文件改了吗"确实需要去查。
    """
    t = (text or "").strip()
    if not t:
        return "chat", "空消息"
    if has_images:
        return "task", "带图片"
    if "```" in t:
        return "task", "含代码块"

    # 问概念（"这是什么意思/为什么"）：凭知识能答，不需要看电脑
    if _CONCEPT_Q.search(t) and not _PATH.search(t):
        return "chat", "问概念"

    # 疑问句优先：除非同时提到了电脑上的具体对象
    if _QUESTION.search(t) and not _PATH.search(t) and not _NOUN.search(t):
        return "chat", "疑问句"

    if _VERB.search(t):
        return "task", "任务动词"
    if _PATH.search(t):
        return "task", "路径"
    if _EN_CMD.search(t):
        return "task", "命令打头"
    if _NOUN.search(t):
        return "task", "提到文件/命令"
    if len(t) > LONG_TEXT:
        return "task", "长文本"
    return "chat", "默认闲聊"
