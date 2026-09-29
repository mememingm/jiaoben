# 开户套餐显示与筛选依据

来源：2026-09-29 读取平台公开前端资源（不登录、不调用业务接口）：
https://admin.nexsimus.com/assets/ActivationCenterView-BeHu0jFW.js

SHA-256：`84ad2b55752c624defad77f04df433e74db0f6241914872ff184a1e1881f7060`

## 编号映射

| 原始 productCode | 平台显示编号 |
| --- | --- |
| P_VOICE_SMS_30M_100 | P001 |
| P_VOICE_SMS_1G_30M_100 | P002 |
| P_ENTITY_003 | P003 |
| P_REGISTRATION_30D | P004 |
| A_SMS_30D | A001 |

这是平台前端明确的映射，不是按套餐名称推测。未知编码不编造简称；原始 productCode 和数字 productId 保留，激活请求仍用原始 productId。

## 平台筛选条件

```javascript
["P_CARD", "A_CARD"].includes(product.cardCategory)
  && product.productPurpose === "BASE_PLAN"
  && product.activationEnabled === 1
```

平台从 `/api/products?orgId=...&operationType=ACTIVATION` 获取数据后仍执行此筛选。本地工具现在执行相同筛选，按 P001、P002、P003、P004、A001 排序，其他符合条件的套餐放后面。缺少上述资格字段的记录不会被推测为可开户。

## 名称、规格与价格

使用接口的 productName、packageSpec、displayPrice，不根据名称或编码编造天数、流量、通话、短信额度。显示短编号、名称、规格及单价；选中项的完整内容换行展示，避免长名称被下拉框截断。同名套餐不会合并，选项始终按 productId 区分。

## 当前工具边界

本工具库存筛选只支持 P 类。A 卡基础套餐展示但禁用；后端读取库存及提交前也拒绝 A 卡开户，不仅依赖前端禁用。历史批次保持原产品 ID；即使产品不再可开户，也可进行只读结果查询，不能静默切换到其他套餐。

前后端以 product_catalog_version=1 标识新列表格式，旧服务需重启后重新识别账号，不用旧的全量套餐列表绕过筛选。

本轮仅对照公开平台源码并修改本地代码，按用户要求未运行自动化测试、浏览器测试或真实开户操作。
