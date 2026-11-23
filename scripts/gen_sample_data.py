"""生成 multi-format 测试数据：模拟电商 SaaS 企业的真实知识库。

为什么要造数据而不是只用那一篇手册：
1. RAG 效果高度依赖数据形态 —— 表格、无结构文本、多页 PDF 的切分行为完全不同
2. 要有"干扰文档"才能测出检索是否真的找对了，而不是随便召回都能对
3. 多租户场景需要两家公司的文档，才能验证隔离是否生效

运行：python3 scripts/gen_sample_data.py
输出：data/ 目录下 6 份文档 + 1 份评测问题集
"""
import json
from pathlib import Path

DATA = Path(__file__).resolve().parent.parent / "data"
if not DATA.exists():
    DATA.mkdir()


def gen_faq_md():
    """短问答型 FAQ：模拟客服知识库，标题层级浅、内容密集。"""
    content = """# 星云电商客服 FAQ

## 账号与登录

### Q: 忘记密码怎么办？
在登录页点击"忘记密码"，输入注册手机号，接收验证码后即可重置。验证码有效期为 5 分钟。

### Q: 一个手机号能注册几个账号？
一个手机号只能注册一个账号。如需多个账号，请使用不同的手机号注册。

## 订单问题

### Q: 如何查询订单物流？
进入"我的订单"页面，点击对应订单的"查看物流"，可看到实时物流轨迹。物流信息更新可能有 2-4 小时延迟。

### Q: 订单可以修改收货地址吗？
订单发货前可在订单详情页修改收货地址一次。发货后无法修改，请联系客服处理。

### Q: 多久发货？
现货商品在付款后 48 小时内发货。预售商品以商品页面标注的发货时间为准。

## 支付问题

### Q: 支持哪些支付方式？
支持微信支付、支付宝、银联在线支付。企业客户可申请对公转账，需联系商务开通。

### Q: 支付失败怎么办？
先确认银行卡余额充足、网络正常。若多次失败，建议更换支付方式或联系支付平台客服。
"""
    (DATA / "客服FAQ.md").write_text(content, encoding="utf-8")


def gen_product_spec_md():
    """产品规格文档：带表格的 Markdown，测试表格内容是否被正确切分保留。"""
    content = """# 星云智能客服 Pro 产品规格

## 产品概述

星云智能客服 Pro 是面向电商企业的 AI 客服系统，支持全渠道消息聚合、智能问答和人工协同。

## 版本对比

| 功能项 | 基础版 | Pro 版 | 旗舰版 |
|--------|--------|--------|--------|
| 知识库文档数 | 100 篇 | 1000 篇 | 不限 |
| 每月 AI 问答次数 | 5000 次 | 50000 次 | 不限 |
| 多渠道接入 | 1 个 | 5 个 | 不限 |
| 工单系统 | 不支持 | 支持 | 支持 |
| 数据看板 | 基础 | 高级 | 定制 |
| 价格（元/月） | 299 | 1299 | 4999 |

## 系统要求

- 浏览器：Chrome 90+、Edge 90+、Safari 14+
- 服务端：Java 11+ 或 Python 3.8+
- 数据库：MySQL 5.7+ 或 PostgreSQL 12+

## SLA 承诺

Pro 版及以上提供 99.9% 可用性 SLA，故障响应时间 30 分钟内。基础版不提供 SLA 保障。
"""
    (DATA / "产品规格.md").write_text(content, encoding="utf-8")


def gen_plain_txt():
    """无结构纯文本：测试没有标题的场景，验证回退到递归切分是否正常。"""
    content = """星云科技成立于 2013 年，总部位于杭州，是一家专注电商 SaaS 领域的企业。
公司主打产品包括智能客服系统、订单管理 ERP、数据分析平台三大产品线。
截至 2025 年底，公司服务超过 4000 家电商企业，日均处理咨询量超过 100 万次。

公司的企业愿景是让每一家电商企业都能用上 AI 技术。核心价值观包括客户第一、技术创新、诚信正直。
研发团队占总人数的 45%，在杭州、深圳两地设有研发中心。

联系方式：客服热线 400-888-6666，工作日 9:00-18:00。商务合作邮箱 business@example.com。
地址：浙江省杭州市滨江区某某路 100 号。
"""
    (DATA / "公司介绍.txt").write_text(content, encoding="utf-8")


def gen_pdf():
    """生成多页 PDF：测试页码提取。注意 PDF 是二进制，不能用 write_text。"""
    try:
        import fitz
    except ImportError:
        print("  跳过 PDF（PyMuPDF 未安装）")
        return
    doc = fitz.open()
    pages = [
        ("退款与售后政策 第一页", "自签收商品之日起七日内，商品保持完好的情况下可申请无理由退款。退款审核通过后 3-5 个工作日到账。定制商品、生鲜商品不支持无理由退款。"),
        ("退款与售后政策 第二页", "商品出现质量问题，买家可在签收后 15 日内申请换货。换货运费由卖家承担。超过 15 日但在质保期内的，按质保政策处理。"),
        ("退款与售后政策 第三页", "售后服务热线 400-888-6666，服务时间工作日 9:00-18:00。紧急问题可通过在线客服提交工单，响应时间 2 小时内。"),
    ]
    for title, body in pages:
        page = doc.new_page()
        # PyMuPDF 默认字体(Base14)不支持中文，insert_text 写中文会变成 "·"；
        # 必须用 insert_htmlbox（1.25+），它会自动处理 CJK 字体。
        page.insert_htmlbox(
            fitz.Rect(50, 60, 550, 110),
            f'<h2 style="font-family:sans-serif">{title}</h2>',
        )
        page.insert_htmlbox(
            fitz.Rect(50, 120, 550, 400),
            f'<p style="font-family:sans-serif;line-height:1.8">{body}</p>',
        )
    # garbage=4 + deflate=True 做字体子集化和压缩，把嵌入字体压到最小
    doc.save(str(DATA / "退款售后政策.pdf"), garbage=4, deflate=True, clean=True)
    doc.close()


def gen_docx():
    """生成 DOCX：测试 Word 解析通道。"""
    try:
        from docx import Document
    except ImportError:
        print("  跳过 DOCX（python-docx 未安装）")
        return
    doc = Document()
    doc.add_heading("员工手册", 0)
    doc.add_heading("考勤制度", level=1)
    doc.add_paragraph("工作时间：周一至周五 9:00-18:00，午休 12:00-13:30。弹性工作制可在 8:30-10:00 之间上班，对应 17:30-19:00 下班。")
    doc.add_paragraph("请假需提前在 OA 系统提交申请。事假 3 天以内由直属主管审批，3 天以上需部门负责人审批。")
    doc.add_heading("福利政策", level=1)
    doc.add_paragraph("五险一金按全额工资缴纳。每年 15 天带薪年假，工作满 5 年增加到 20 天。")
    doc.add_paragraph("年度体检、节日福利、团队建设经费每人每年 2000 元。")
    doc.save(str(DATA / "员工手册.docx"))


def gen_eval_questions():
    """评测问题集：带标准答案，后面任务 4/5 用来量化检索和生成质量。"""
    questions = [
        {"q": "七天无理由退款的条件是什么？", "answer": "签收起七日内，商品完好，定制和生鲜等例外商品除外", "source": "退款售后政策.pdf", "type": "单文档定位"},
        {"q": "商品有质量问题多久内可以换货？", "answer": "签收后 15 日内", "source": "退款售后政策.pdf", "type": "单文档定位"},
        {"q": "忘记密码如何重置？", "answer": "登录页点击忘记密码，输入手机号收验证码，5 分钟内有效", "source": "客服FAQ.md", "type": "FAQ 匹配"},
        {"q": "Pro 版每月多少次 AI 问答？", "answer": "50000 次", "source": "产品规格.md", "type": "表格定位"},
        {"q": "旗舰版价格是多少？", "answer": "4999 元/月", "source": "产品规格.md", "type": "表格定位"},
        {"q": "公司客服热线是多少？", "answer": "400-888-6666", "source": "公司介绍.txt", "type": "无结构文本"},
        {"q": "事假 3 天以上谁审批？", "answer": "部门负责人", "source": "员工手册.docx", "type": "DOCX 定位"},
        {"q": "预售商品什么时候发货？", "answer": "以商品页面标注的发货时间为准", "source": "客服FAQ.md", "type": "FAQ 匹配"},
        {"q": "Pro 版的 SLA 可用性承诺是多少？", "answer": "99.9%", "source": "产品规格.md", "type": "单文档定位"},
        {"q": "年休假工作满 5 年有多少天？", "answer": "20 天", "source": "员工手册.docx", "type": "DOCX 定位"},
    ]
    (DATA / "eval_questions.json").write_text(
        json.dumps(questions, ensure_ascii=False, indent=2), encoding="utf-8"
    )


if __name__ == "__main__":
    gen_faq_md()
    gen_product_spec_md()
    gen_plain_txt()
    gen_pdf()
    gen_docx()
    gen_eval_questions()
    print("测试数据生成完成：")
    for f in sorted(DATA.iterdir()):
        if f.is_file():
            print(f"  {f.name}  ({f.stat().st_size} bytes)")
