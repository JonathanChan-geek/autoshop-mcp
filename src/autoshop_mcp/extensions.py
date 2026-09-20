"""Tool metadata for analysis and transactional workflows."""
from . import analysis, workflows


def arg(kind, help, required=False, **extra):
    return dict(type=kind, help=help, required=required, **extra)


PROJECT = arg('str', '含唯一 HCP 索引的完整工程目录', True)
DEST = arg('str', '源工程之外、尚不存在的新目标目录', True)
INSTALL = arg('str', 'AutoShop 安装目录；省略时读取 AUTOSHOP_INSTALL_DIR')
EDIT_SCHEMA = {'type': 'object', 'properties': {'old_text': {'type':'string','minLength':1},
                                             'new_text': {'type':'string'}},
               'required':['old_text','new_text'], 'additionalProperties':False}
PATCH_SCHEMA = {'type':'object', 'properties': {'file':{'type':'string'},
                 'expected_sha256':{'type':'string','pattern':'^[a-fA-F0-9]{64}$'},
                 'edits':{'type':'array','items':EDIT_SCHEMA,'minItems':1,'maxItems':100}},
                'required':['file','expected_sha256','edits'], 'additionalProperties':False}
PATCHES = arg('list', '按文件组织的原哈希和 edits；所有匹配基于修改前文本，禁止重叠',
              items=PATCH_SCHEMA, minItems=1, maxItems=100)


def spec(name, handler, summary, **params):
    return dict(name=name, handler=handler, summary=summary, params=params)


TOOLS = [
    spec('project_search', analysis.project_search, '搜索已登记 IL 的字面文本，返回文件、行号和上下文；明确未覆盖的 LD/保护块。',
         project=PROJECT, query=arg('str','查找的字面文本，不是正则表达式',True),
         case_sensitive=arg('bool','是否区分大小写，默认 false'),
         limit=arg('int','最多返回的匹配行，默认 200',minimum=1,maximum=2000),
         context=arg('int','每侧上下文行数，默认 2',minimum=0,maximum=10)),
    spec('project_xref', analysis.project_xref, '列出显式软元件地址的指令和读写分类；范围、隐式双字和间接地址不作完整性保证。',
         project=PROJECT, device=arg('str','可选直接地址，例如 X10 或 D500；省略则列全部')),
    spec('project_audit', analysis.project_audit, '静态检查缺失文件、多处 OUT、跨块写入和未知指令；不是运行仿真或逻辑合格证明。',
         project=PROJECT),
    spec('project_export', analysis.project_export, '导出 UTF-8 IL 文本、哈希、引用和静态检查 JSON 到新目录，供审查。',
         project=PROJECT, dest=DEST),
    spec('il_batch_patch_copy', workflows.il_batch_patch_copy, '一次核对多文件、多处非重叠补丁；成功才发布副本，隔离旧产物并核对其余文件。',
         project=PROJECT, dest=DEST, patches=dict(PATCHES,required=True)),
    spec('project_convert_all_copy', workflows.project_convert_all_copy, '将工程内全部 LD 依次转成 IL，每次验证机器码等价，最后统一编译交付。',
         project=PROJECT, dest=DEST, install_dir=INSTALL),
    spec('project_build_copy', workflows.project_build_copy, '可选全部 LD 转换、多文件补丁、原厂编译和打包的一次调用；失败不发布中间工程。',
         project=PROJECT, dest=DEST, patches=PATCHES,
         convert_all=arg('bool','是否先将全部 LD 转为 IL，默认 false；补丁哈希必须匹配转换后的 IL'),
         install_dir=INSTALL),
]
