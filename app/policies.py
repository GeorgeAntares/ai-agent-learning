import json
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Query
from pydantic import BaseModel, field_validator

router = APIRouter(prefix="/internal/policies", tags=["internal-policies"])

_POLICY_FILE = Path(__file__).parent / "data" / "policies.json"


class PolicyQuery(BaseModel):
    """定义政策检索服务接受的已校验用户问题。

    做什么：保存用户的政策查询文本，并拒绝空字符串和纯空格。
    为什么：政策服务只能根据实际问题匹配本地依据，空文本没有可检索含义。
    结果：路由和未来 Agent 都向服务传递统一、有效的查询模型。
    """

    query: str

    @field_validator("query")
    @classmethod
    def validate_query(cls, value: str) -> str:
        # 做什么：拒绝仅由空白字符组成的政策查询文本。
        # 为什么：空白文本不应触发文件加载或产生没有依据的政策结果。
        # 结果：无效输入在进入服务前触发 Pydantic 校验错误。
        if not value.strip():
            raise ValueError("query must not be blank")
        return value


class PolicyRecord(BaseModel):
    """定义政策知识文件中一条可检索的完整记录。

    做什么：校验政策编号、标题、正文和关键词列表的本地文件结构。
    为什么：文件内容是检索依据，加载时发现结构错误比运行中静默返回错误结果更可靠。
    结果：服务只使用字段完整且类型正确的本地政策记录进行匹配。
    """

    policy_id: str
    title: str
    content: str
    keywords: list[str]


class PolicySearchResult(BaseModel):
    """定义单条可供后续引用的政策检索结果。

    做什么：返回政策标识、标题和未经生成的原文片段。
    为什么：调用方需要可追溯依据，而不是服务重新组织或编造自然语言答案。
    结果：snippet 始终来自本地政策 content 的原始文本。
    """

    policy_id: str
    title: str
    snippet: str


class PolicySearchResponse(BaseModel):
    """定义内部政策检索路由的固定响应结构。

    做什么：组合原始查询文本与命中的政策引用结果。
    为什么：路由需要稳定区分没有依据的空结果和已命中的可引用政策。
    结果：无匹配时仅 results 为空列表，不返回猜测性答案。
    """

    query: str
    results: list[PolicySearchResult]




class PolicyService:
    """封装本地政策知识文件的确定性检索入口。

    做什么：读取政策文件、按关键词匹配政策并返回原文引用片段。
    为什么：API 路由和未来 Agent 不能直接读取文件，避免产生不一致的检索规则。
    结果：所有政策检索都复用同一文件、匹配门槛和类型固定的返回模型。
    """

    def __init__(self, policy_file: Path = _POLICY_FILE) -> None:
        # 做什么：读取并校验本地 JSON 政策知识文件。
        # 为什么：服务初始化时固定检索依据，避免每次查询重复读取文件或接受未校验的字典数据。
        # 结果：后续 search_policy 只在内存中的受类型约束政策记录中执行匹配。
        raw_policies = json.loads(policy_file.read_text(encoding="utf-8"))
        self._policies = [PolicyRecord.model_validate(policy) for policy in raw_policies]

    def search_policy(self, query: PolicyQuery) -> list[PolicySearchResult]:
        # 做什么：根据查询中的政策关键词筛选记录，并返回命中政策的原文内容。
        # 为什么：至少命中两个关键词可避免火星退货规则仅因包含通用词退货而误匹配本地政策。
        # 结果：命中记录按知识文件顺序返回；没有足够依据时返回空列表而不猜测答案。
        normalized_query = query.query.strip().casefold()
        results: list[PolicySearchResult] = []
        for policy in self._policies:
            keyword_hits = sum(
                keyword.casefold() in normalized_query for keyword in policy.keywords
            )
            full_text = f"{policy.title}\n{policy.content}".casefold()
            if keyword_hits >= 2 or normalized_query in full_text:
                results.append(
                    PolicySearchResult(
                        policy_id=policy.policy_id,
                        title=policy.title,
                        snippet=policy.content,
                    )
                )
        return results
    
@router.get("/search", response_model=PolicySearchResponse)
def search_internal_policies(
    query: Annotated[str, Query(min_length=1)],
) -> PolicySearchResponse:
    # 做什么：提供 Swagger 和本地验收使用的内部政策检索入口。
    # 为什么：路由只负责 HTTP 参数和响应包装，政策文件读取与匹配必须委托给 PolicyService。
    # 结果：调用方获得可引用政策结果或明确的空列表，不会得到服务编造的结论。
    policy_query = PolicyQuery(query=query)
    results = PolicyService().search_policy(policy_query)
    return PolicySearchResponse(query=policy_query.query, results=results)
