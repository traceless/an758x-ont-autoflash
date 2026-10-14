"""机型 profile：集中声明各机型差异，命令层按字段 dispatch，避免散落 if-else。

新增机型只需在此追加一个条目；固件放 {uboot_dir}/（文件名含 preloader / fip / .itb
关键字即被自动识别，无需改代码）。

字段说明：
  name          机型展示名（含运营商）
  uboot_dir     固件目录（preloader/fip/sysupgrade.itb 所在）
  default_user  Web 超管账号（仅作交互输入的回车默认值）
  default_pass  Web 超管密码（仅作交互输入的回车默认值）
  web_port      Web 登录（/login.cgi）端口
  mgmt_port     管理 CGI（system.cgi / dumpdatamodel.cgi）端口
  telnet_open   开 telnet 方式：cgi_toggle=MD（POST telnet+off/on 空 body）；
                cgi_plain=TF/MF（POST /system.cgi?telnet+off/on，body=data&csrf_token=）
  supass_mode   取超密方式：log_plaintext=日志包明文；dumpdatamodel_aes=dumpdatamodel 密文 AES 解密
  telnet_login  telnet 登录取向：user_su=user/useradmin + su；root=root + 超密直登
"""

DEFAULT_MODEL = "MD"

DEVICE_MODELS = {
    "MD": {
        "name": "诺基亚 XG-040G-MD（移动）",
        "uboot_dir": "uboot-md",
        "default_user": "CMCCAdmin",
        "default_pass": "aDm8H%MdA",
        "web_port": 80,
        "mgmt_port": 80,
        "telnet_open": "cgi_toggle",
        "supass_mode": "log_plaintext",
        "telnet_login": "user_su",
    },
    "TF": {
        "name": "诺基亚 XG-040G-TF（电信）",
        "uboot_dir": "uboot-tf",
        "default_user": "telecomadmin",
        "default_pass": "nE7jA%5m",
        "web_port": 8080,  # 实测：80 是 LuCI（useradmin 用），超管 telecomadmin 走 :8080 goahead（MD 同款 login.cgi）
        "mgmt_port": 8080,  # 实测：system.cgi / dumpdatamodel.cgi 均在 :8080
        "telnet_open": "cgi_plain",
        "supass_mode": "dumpdatamodel_aes",
        "telnet_login": "root",  # root + 超密直登，无需贴纸账号
    },
    # MF 实测（XG-040G-MF 真机 2026-10-14 验证）：
    #   登录 :80 goahead，表单 name/pswd，明文提交即返回 299 + sid；
    #   开 telnet = POST /system.cgi?telnet+off/on（body=data&csrf_token=，与 TF 同构，空 token 亦可）；
    #   取超密 = dumpdatamodel.cgi 两步（页面 csrf_token → POST ?data）→ AES 解密，密钥与 TF 同份；
    #   telnet = root + 解出的超密直登（uid=0，直接 # 提示符）。
    "MF": {
        "name": "诺基亚 XG-040G-MF（移动）",
        "uboot_dir": "uboot-mf",
        "default_user": "CMCCAdmin",
        "default_pass": "aDm8H%MdA",
        "web_port": 80,
        "mgmt_port": 80,
        "telnet_open": "cgi_plain",
        "supass_mode": "dumpdatamodel_aes",
        "telnet_login": "root",
    },
}


def get_profile(model):
    """取机型 profile；未知机型回退默认。"""
    return DEVICE_MODELS.get(model) or DEVICE_MODELS[DEFAULT_MODEL]
