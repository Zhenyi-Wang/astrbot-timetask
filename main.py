import json
import os
import random
import string
from datetime import datetime
from typing import Optional
from uuid import uuid4
from datetime import timedelta
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.date import DateTrigger

from astrbot.core.message.message_event_result import MessageChain
from astrbot.core.platform.sources.wechatpadpro.wechatpadpro_adapter import WeChatPadProAdapter
from astrbot.core.platform.message_type import MessageType

from astrbot.api.event import filter, AstrMessageEvent, MessageEventResult
from astrbot.api.star import Context, Star, register
from astrbot.api import logger

@register("timetask", "ZW", "定时发送消息到指定群聊。用法: /time <时间> [GPT] <内容> [<群名>]", "v0.1")
class MyPlugin(Star):
    def __init__(self, context: Context):
        super().__init__(context)
        self.scheduler = AsyncIOScheduler(timezone="Asia/Shanghai")
        
        # 创建目录
        if not os.path.exists("data/timetask"):
            os.makedirs("data/timetask")
        
        # 创建数据文件
        if not os.path.exists("data/timetask/tasks.json"):
            with open("data/timetask/tasks.json", "w", encoding="utf-8") as f:
                f.write("{}")
                logger.info("没有找到任务配置文件，创建data/timetask/tasks.json")
    
        with open("data/timetask/tasks.json", "r", encoding="utf-8") as f:
            logger.debug("加载任务配置文件")
            tasks_data = json.load(f)
            
            # 过滤掉过期的任务
            current_time = datetime.now()
            self.tasks = {}
            for msg_origin, tasks in tasks_data.items():
                self.tasks[msg_origin] = []
                for task in tasks:
                    # 如果是一次性任务，需要判断是否过期
                    if "datetime" in task:
                        # 具体日期时间，如 "2025-03-30 16:30"
                        schedule_time = datetime.strptime(task['datetime'], "%Y-%m-%d %H:%M")
                        if schedule_time > current_time:
                            self.tasks[msg_origin].append(task)
                        else:
                            logger.info(f"任务 {task['id']} 已过期，从配置中移除")
                            continue
                    else:
                        # 其他情况直接添加
                        self.tasks[msg_origin].append(task)
            logger.debug(f"加载任务配置(已过滤过期任务): {self.tasks}")
            
            self._save_tasks(self.tasks)
                
        # 加载任务到调度器
        self._load_tasks()
        self.scheduler.start()
    
    def _load_tasks(self):
        """加载保存的定时任务"""            
        for msg_origin, tasks in self.tasks.items():
            for task in tasks:
                self._schedule_task(msg_origin, task)
    
    def _save_tasks(self, tasks):
        """保存任务配置到文件"""
        # 先清除任务列表为空的对象
        # tasks = {msg_origin: tasks for msg_origin, tasks in tasks.items() if tasks}
        
        with open("data/timetask/tasks.json", "w", encoding="utf-8") as f:
            json.dump(tasks, f, ensure_ascii=False, indent=2)
    
    def _schedule_task(self, msg_origin, task):
        """添加定时任务到调度器"""
        trigger = CronTrigger.from_crontab(task["cron"]) if "cron" in task else \
                 DateTrigger(run_date=datetime.strptime(task["datetime"], "%Y-%m-%d %H:%M"))
                 
        logger.debug(f"添加定时任务: msg_origin={msg_origin}, task={task}")
        logger.debug(f"trigger={trigger}")
                 
        self.scheduler.add_job(
            self._send_message,
            trigger=trigger,
            args=[msg_origin, task],
            id=task["id"],
            misfire_grace_time=60
        )
        logger.debug(f"添加定时任务: msg_origin={msg_origin}, task={task}")
    
    async def _send_message(self, msg_origin: str, task: dict):
        """发送消息"""
        
        content = task["content"]
        # 如果需要使用GPT
        if task["use_gpt"]:
            providers = self.context.get_all_providers()
            # 如果没有可用的Provider
            if not providers:
                logger.error("没有可用的Provider")
                content = "Error: 没有可用的LLM服务"
            else:
                provider = providers[0]
                response = await provider.text_chat(content)
                if not response.completion_text:
                    logger.error("无法获取回复", response.raw_completion)
                    content = "Error: 无法获取回复"
                else:
                    content = response.completion_text or "Error: 回复为空"
        
        await self.context.send_message(msg_origin, MessageChain().message(content))

        # 如果是一次性任务（使用datetime而不是cron），发送后删除
        if "datetime" in task:
            # 从tasks中删除该任务
            if msg_origin in self.tasks:
                self.tasks[msg_origin] = [t for t in self.tasks[msg_origin] if t["id"] != task["id"]]
                
            # 保存到文件
            self._save_tasks(self.tasks)
    
    def _parse_datetime(self, date_str: str, time_str: str) -> tuple[Optional[str], Optional[str]]:
        """解析日期时间字符串，返回(cron表达式, 人类可读描述)的元组"""
        time_parts = time_str.split(":")
        if len(time_parts) != 2:
            return None, None
        hour, minute = time_parts
        
        if not (hour.isdigit() and minute.isdigit()):
            return None, None
            
        if not (0 <= int(hour) <= 23 and 0 <= int(minute) <= 59):
            return None, None
            
        # 处理描述性日期
        if date_str == "每天":
            return f"{minute} {hour} * * *", f"每天{hour}:{minute}"
        elif date_str == "工作日":
            return f"{minute} {hour} * * 0-4", f"每个工作日{hour}:{minute}"
        elif date_str.startswith("每周"):
            weekday_map = {"一": "0", "二": "1", "三": "2", "四": "3", "五": "4", "六": "5", "日": "6", "天": "6"}
            weekday_names = {"一": "一", "二": "二", "三": "三", "四": "四", "五": "五", "六": "六", "日": "日", "天": "日"}
            weekday = date_str[2:]
            if weekday not in weekday_map:
                return None, None
            return f"{minute} {hour} * * {weekday_map[weekday]}", f"每周{weekday_names[weekday]}{hour}:{minute}"
        else:
            # 处理具体日期
            try:
                if date_str in ["今天", "明天", "后天"]:
                    today = datetime.now()
                    days_to_add = {"今天": 0, "明天": 1, "后天": 2}[date_str]
                    target_date = today.replace(hour=int(hour), minute=int(minute)) + timedelta(days=days_to_add)
                else:
                    target_date = datetime.strptime(f"{date_str} {time_str}", "%Y-%m-%d %H:%M")
                return target_date.strftime("%Y-%m-%d %H:%M"), target_date.strftime("%Y年%m月%d日 %H:%M")
            except ValueError:
                return None, None

    def _parse_command(self, instruct: str) -> Optional[dict]:
        """解析命令字符串，返回解析后的参数字典"""
        # 移除前后空格
        instruct = instruct.strip()
        
        # 初始化结果字典
        result = {
            "use_gpt": False,
            "group_name": None,
            "content": None,
            "schedule": None,
            "schedule_h": None  # 添加人类可读的时间描述
        }
        
        # 解析群名（如果存在）
        if "group[" in instruct:
            try:
                group_start = instruct.rindex("group[")
                group_end = instruct.rindex("]")
                if group_start < group_end:
                    result["group_name"] = instruct[group_start+6:group_end]
                    instruct = instruct[:group_start].strip()
            except ValueError:
                return None
        
        # 检查是否使用GPT
        parts = instruct.split(" ")
        logger.debug(f"解析前的指令：{instruct}")
        logger.debug(f"解析后的指令：{parts}")
        if "GPT" in parts:
            result["use_gpt"] = True
            parts.remove("GPT")
            instruct = " ".join(parts)
        
        # 解析cron表达式
        if instruct.startswith("cron[") and "]" in instruct:
            cron_end = instruct.index("]")
            cron_expr = instruct[5:cron_end]
            try:
                # 验证cron表达式
                CronTrigger.from_crontab(cron_expr)
                result["schedule"] = cron_expr
                result["schedule_h"] = instruct[5:cron_end]  # 保存原始表达式作为描述
                result["content"] = instruct[cron_end+1:].strip()
                return result
            except Exception:
                return None
        
        # 解析日期时间格式
        parts = instruct.split(" ",maxsplit=2)
        if len(parts) < 3:
            return None
            
        date_str, time_str, content = parts
        schedule, schedule_h = self._parse_datetime(date_str, time_str)
        if not schedule:
            return None
            
        result["schedule"] = schedule
        result["schedule_h"] = schedule_h
        result["content"] = content
        return result

    @filter.command_group("time")
    def time(self):
        """定时任务命令组"""
        pass

    @filter.command("time")
    async def time_main(self, event: AstrMessageEvent):
        """定时发送消息到群聊
        用法语法解释：<> 表示必须填，() 表示可选
        用法1: /time <日期 时间> (GPT) <内容> (group[群名])
        用法2: /time cron[<表达式>] (GPT) <内容> (group[群名])
        时间格式：
        - 日期时间
            - 具体日期: "2023-12-31"
            - 每天: "每天"
            - 每周几: "每周一"、"每周二"、"每周三"、"每周四"、"每周五"、"每周六"、"每周日"
        - 时间: "10:30"
        - cron表达式: "0 * * * *"
            - 分钟 (0-59)
            - 小时 (0-23)
            - 日期 (1-31)
            - 月份 (1-12)
            - 星期 (0-6) (星期一=0)
        注意事项：
        - 群必须在你的联系人列表中
        - 群名是群的名称，不是sid
        示例：
        - 每天早上用固定的文字问候：/time 每天 10:30 早上好！

        - 每天早上让AI用猫娘的语气问候：/time 每天 10:30 GPT 现在是10:30，用猫娘的语气对我说早上好

        - 每天早上向群里发送消息：/time 每天 10:30 滴滴滴 group[群名]健身

        - 准点报时：/time cron[0 * * * *] 滴滴滴~~

        - 每周三夸一夸我：/time 每周三 10:30 GPT 夸一夸我
        """
        # 获取消息字符串
        message_str = event.message_str
        
        # 过滤其他命令，例如 time rm, time ls, time help
        COMMAND = "time"
        SUB_COMMANDS = ["rm", "ls", "help"]
        if any(message_str.startswith(f"{COMMAND} {sub_command}") for sub_command in SUB_COMMANDS):
            return
        
        # 解析命令
        instruct = message_str[len(COMMAND):].strip()
        parsed = self._parse_command(instruct)
        
        # 验证命令
        if not parsed:
            yield event.plain_result("命令格式错误，请检查语法")
            return
            
        if not parsed["content"]:
            yield event.plain_result("消息内容不能为空")
            return

        # 获取平台
        platform_name = event.get_platform_name()        
        
        group_id = None
        if parsed["group_name"]:
            
            # 判断平台类型，非wechatpadpro提示不支持
            if platform_name != "wechatpadpro":
                yield event.plain_result("暂时只有wechatpadpro支持群任务")
                return
            
            # 获取平台和客户端
            platform : WeChatPadProAdapter = self.context.get_platform(platform_name)
            
            # 判断get_contact_list是否支持
            if not hasattr(platform, "get_contact_list") or not hasattr(platform, "get_contact_details_list"):
                yield event.plain_result("请升级到最新版AstrBot以支持群任务")
                return
            
            
            # 获取联系人id列表
            contact_ids = await platform.get_contact_list()
            
            if not contact_ids:
                logger.warning("获取联系人id列表失败")
                yield event.plain_result(f"未找到名为 {parsed['group_name']} 的群(获取联系人id列表为空)")
                return
            
            contact_details = await platform.get_contact_details_list([], contact_ids)
            
            if not contact_details:
                logger.warning("获取联系人信息失败")
                yield event.plain_result(f"未找到名为 {parsed['group_name']} 的群(获取联系人信息为空)")
                return
            
            for contact in contact_details:
                contact_name = contact.get("nickName", {}).get('str', '')
                if contact_name == parsed["group_name"]:
                    group_id = contact.get("userName", {}).get('str', '')
                    break
            
            if not group_id:
                yield event.plain_result(f"未找到名为 {parsed['group_name']} 的群")
                return
            
            
        # 生成任务ID
        while True:
            task_id = "".join(random.choices(string.digits, k=4))
            if not any(task["id"] == task_id for tasks in self.tasks.values() for task in tasks):
                break

        # 创建任务配置
        task = {
            "id": task_id,
            "content": parsed["content"],
            "use_gpt": parsed["use_gpt"],
            "group_name": parsed["group_name"],
            "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        }
        
        # 根据schedule类型设置触发器
        if isinstance(parsed["schedule"], str) and ":" in parsed["schedule"]:
            # 具体日期时间，如 "2025-03-30 16:30"
            schedule_time = datetime.strptime(parsed["schedule"], "%Y-%m-%d %H:%M")
            current_time = datetime.now()
            if schedule_time < current_time:
                yield event.plain_result(f"设置的时间 {parsed['schedule']} 早于当前时间，请设置未来的时间")
                return
            
            task["datetime"] = parsed["schedule"]
            task["datetime_h"] = parsed["schedule_h"]
        else:
            # cron表达式
            task["cron"] = parsed["schedule"]
            task["cron_h"] = self.humanize_cron(parsed["schedule"])
            
        # 使用event的统一消息来源，如果指定了群，则使用群消息来源
        msg_origin = event.unified_msg_origin
        if parsed["group_name"]:
            msg_origin = f"{platform_name}:{MessageType.GROUP_MESSAGE.value}:{group_id}"
         
        # 保存任务
        if msg_origin not in self.tasks:
            self.tasks[msg_origin] = []
        self.tasks[msg_origin].append(task)
        self._save_tasks(self.tasks)
        
        logger.debug(f"新任务: msg_origin={msg_origin}, task={task}")
        
        # 添加到调度器
        self._schedule_task(msg_origin, task)
        
        # 返回成功消息
        schedule_type = "cron表达式" if "cron" in task else "具体时间"
        schedule_value = task.get("cron", task.get("datetime"))
        schedule_h = task.get("cron_h", task.get("datetime"))
        response = f"定时任务创建成功！\n" \
                  f"任务ID: {task_id}\n" \
                  f"触发方式: {schedule_type}\n" \
                  f"触发值: {schedule_value}\n" \
                  f"触发描述: {schedule_h}\n" \
                  f"使用GPT: {'是' if parsed['use_gpt'] else '否'}\n"
        if parsed["group_name"]:
            response += f"目标群组: {parsed['group_name']}\n"
        response += f"消息内容: {parsed['content']}"
        
        yield event.plain_result(response)
    
    def humanize_cron(self, cron_str: str):
        """
        使用 cron_descriptor 将 cron 表达式转换为人类可读的格式，如果转换失败则返回原始 cron 表达式
        """
        try:
            from cron_descriptor import ExpressionDescriptor, Options, CasingTypeEnum
            # 创建选项对象
            options = Options()
            options.locale_code = 'zh_CN'  # 设置语言为中文
            options.use_24hour_time_format = True  # 使用24小时制
            options.casing_type = CasingTypeEnum.Sentence  # 使用句子格式
            
            # 创建描述器并获取描述
            descriptor = ExpressionDescriptor(cron_str, options)
            desc = descriptor.get_description()
            
            # 替换英文星期、月份为中文
            # 特别注意，APScheduler 的 cron 格式是差一天的，所以需要特殊处理
            weekday_map = {
                "Sunday": "周一",
                "Monday": "周二",
                "Tuesday": "周三",
                "Wednesday": "周四",
                "Thursday": "周五",
                "Friday": "周六",
                "Saturday": "周日"
            }
            
            month_map = {
                "January": "一月",
                "February": "二月",
                "March": "三月",
                "April": "四月",
                "May": "五月",
                "June": "六月",
                "July": "七月",
                "August": "八月",
                "September": "九月",
                "October": "十月",
                "November": "十一月",
                "December": "十二月"
            }
            
            for en, zh in weekday_map.items():
                desc = desc.replace(en, zh)
            
            for en, zh in month_map.items():
                desc = desc.replace(en, zh)
                
            return f"循环<{desc}>"
        except Exception:
            return f"cron[{cron_str}]"
    
    # 有用的信息记录：    
    # 发送消息方式1：
    # await client.post_text(group_id, 'test')
    # 
    # 发送消息方式2：
    # new_msg_origin = f"{platform_name}:{MessageType.GROUP_MESSAGEvalue}:{group_id}"
    # await self.context.send_message(new_msg_origin, MessageChain().message(content))
    
    # origin格式如下：
    # gewechat:FriendMessage:wxid_12345
    # gewechat:GroupMessage:12345@chatroom
    
    # 获取会话管理器
    # self.context.conversation_manager
    
    #### 测试用例
    # 基本时间格式测试：
    # # 具体日期和时间
    # /time 2025-03-30 16:30 明天下午的提醒

    # # 每天定时
    # /time 每天 08:00 早上好

    # # 工作日定时
    # /time 工作日 09:00 该上班了

    # # 每周定时（测试不同星期）
    # /time 每周一 07:30 新的一周开始了
    # /time 每周三 12:00 周三午饭提醒
    # /time 每周日 20:00 周末总结

    # # 相对日期
    # /time 今天 16:00 今天下午四点提醒
    # /time 明天 10:00 明天上午提醒
    # /time 后天 20:00 后天晚上提醒
    
    # GPT 功能测试：
    # # 普通GPT对话
    # /time 每天 09:00 GPT 用阳光温暖的语气说早安

    # # 带特定要求的GPT对话
    # /time 每周一 08:30 GPT 用励志的语气说一句激励的话

    # # 复杂GPT提示
    # /time 每天 12:00 GPT 扮演一位营养师，提供一条健康的饮食建议

    # 群组功能测试：
    # # 普通群消息
    # /time 每天 10:00 早上好 group[测试群]

    # # 群消息带GPT
    # /time 每天 20:00 GPT 总结今天的新闻要点 group[新闻群]

    # # 特定时间的群提醒
    # /time 工作日 09:30 今天的工作安排 group[工作群]
    # cron表达式测试：
    
    # # 每小时整点
    # /time cron[0 * * * *] 整点报时

    # # 每天特定时间段
    # /time cron[0 9-18 * * *] 工作时间提醒

    # # 每周特定时间
    # /time cron[0 10 * * 1-5] 工作日上午提醒

    # # 复杂cron表达式
    # /time cron[*/30 9-18 * * 1-5] 工作日每半小时提醒
    
    # 组合功能测试：
    # # cron + GPT + 群组
    # /time cron[0 12 * * *] GPT 午间天气预报 group[天气群]

    # # 每周 + GPT + 群组
    # /time 每周五 17:30 GPT 总结本周工作，提出下周计划 group[工作群]

    # # 每天 + GPT + 多个参数
    # /time 每天 21:00 GPT 用轻松的语气总结今天发生的有趣事情 group[日记群]
    # 边界测试：
    # CopyInsert
    # # 时间格式边界
    # /time 每天 00:00 凌晨提醒
    # /time 每天 23:59 深夜提醒

    # # 最短内容
    # /time 每天 12:00 hi

    # # 较长内容
    # /time 每天 15:00 这是一个很长的提醒内容，包含了很多信息，测试长文本的处理能力，看看是否能正确保存和显示这么长的内容。

    # # 特殊字符
    # /time 每天 08:00 提醒：今天要做：1. 看邮件 2. 开会 3. 写报告！@#$%
    
    # async def find_group_by_name(self, group_name: str, platform: WeChatPadProAdapter) -> Optional[str]:      
    #     # 获取联系人id列表
    #     contact_ids = await platform.get_contact_list()
        
    #     if not contact_ids:
    #         return None
        
    #     # 获取联系人信息
    #     contact_details = await platform.get_contact_details_list([], contact_ids)
        
    #     if not contact_details:
    #         logger.warning(f"获取联系人信息失败, contact_ids: {contact_ids}, contact_details: {contact_details}")
    #         return None
    #     for contact in contact_details:
    #         if contact.get("nickName", "") == group_name:
    #             return contact.get("userName", "")
    #     return None
    
    async def terminate(self):
        """停止调度器"""
        self.scheduler.shutdown()
        pass

    @time.command("ls")
    async def list_tasks(self, event: AstrMessageEvent):
        """列出所有定时任务"""
        tasks_list = []
        for msg_origin, tasks in self.tasks.items():
            tasks_list.extend(tasks)
            
        if not tasks_list:
            yield event.plain_result("当前没有定时任务")
            return
            
        response = "当前的定时任务：\n"
        for task in tasks_list:
            # schedule_type = "cron表达式" if "cron" in task else "具体时间"
            schedule_h = task.get("cron_h", task.get("datetime"))            
            response += f"[{task['id']}] {schedule_h}"
            if task.get('use_gpt'):
                response += " GPT：" 
            response += f" {task.get('content')}"
            if task.get('group_name'):  
                response += f" 发到群<{task['group_name']}>"
            response += "\n"
            
        yield event.plain_result(response.rstrip())

    @time.command("rm")
    async def remove_task(self, event: AstrMessageEvent):
        """删除定时任务
        用法: /time rm <任务ID1> [任务ID2 ...]
        示例: 
        - /time rm 1234  # 删除单个任务
        - /time rm 1234 5678  # 删除多个任务
        """
        user_msg = event.get_message_str()
        ids_to_remove = user_msg.split()[2:]
        logger.debug(f"user_msg: {user_msg}")
        logger.debug(f"要删除的任务ID: {ids_to_remove}")
        
        if not ids_to_remove:
            yield event.plain_result("请提供要删除的任务ID，格式：/time rm <任务ID1> [任务ID2 ...]")
            return
        
        # 记录删除结果
        removed_ids = []
        not_found_ids = []
        
        # 删除每个任务
        for task_id in ids_to_remove:
            # 查找要删除的任务
            target_origin = None
            target_task = None
            for msg_origin, tasks in self.tasks.items():
                for task in tasks:
                    if task["id"] == task_id:
                        target_origin = msg_origin
                        target_task = task
                        break
                if target_origin:
                    break
                    
            if not target_origin:
                not_found_ids.append(task_id)
                continue
                
            # 删除任务
            self.tasks[target_origin].remove(target_task)
            if not self.tasks[target_origin]:
                del self.tasks[target_origin]  # 如果没有任务了，删除这个渠道
                
            # 也从scheduler中删除
            self.scheduler.remove_job(task_id)
            
            removed_ids.append(task_id)
        
        # 保存更新后的任务
        if removed_ids:
            self._save_tasks(self.tasks)
            logger.debug(f"删除任务: {removed_ids}")
            logger.debug(f"当前任务配置: {self.tasks}")
        
        # 返回成功消息
        response = []
        if removed_ids:
            response.append(f"已删除任务：{', '.join(removed_ids)}")
        if not_found_ids:
            response.append(f"未找到任务：{', '.join(not_found_ids)}")
            
        yield event.plain_result("\n".join(response))

    @time.command("help")
    async def show_help(self, event: AstrMessageEvent):
        yield event.plain_result("""AstrBot 定时任务插件 - 常用命令

【创建任务】
/time 每天 08:00 早安！
/time 每周五 17:30 周末愉快！
/time cron[0 12 * * *] 午饭时间到！

【使用GPT】
/time 每天 09:00 GPT 说一句励志的话
/time 每周一 08:30 GPT 用猫娘语气说早安

【群组消息】
/time 每天 10:00 开始会议！ group[工作群]
/time 周五 18:00 GPT 周末祝福 group[亲友群]

【管理任务】
/time ls    # 查看任务
/time rm 123 # 删除任务

注：群聊功能目前仅支持WechatPadPro平台""")