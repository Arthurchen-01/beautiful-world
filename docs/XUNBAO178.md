# 寻宝178 (xunbao178.com) 侦察报告

> 用户提供的入口：`http://www.xunbao178.com/games/jsp/index.jsp`
> 结论：**它是商品交易站，不是账号查区工具。不能替代按账号查区的接口。**

---

## 一、站点性质

**「寻宝天行 / 寻宝完美手游」—— 完美世界官方合作虚拟物品交易平台**

页面标题原文：
```
完美世界官方合作虚拟物品交易伙伴----寻宝天行
寻宝完美手游-完美世界官方合作交易平台
```

它是个**游戏账号/道具交易市场**，不是账号信息查询工具。

---

## 二、站点结构（实测）

| 入口 | 内容 |
|:--|:--|
| `www.xunbao178.com/games/jsp/index.jsp` | 主站首页（腾讯系游戏） |
| `hot.xunbao178.com/` | **完美世界专区首页**（完美手游 + 完美端游） |
| `hot.xunbao178.com/jsp/shop/gameZone.jsp` | 交易区服页 |
| `hot.xunbao178.com/jsp/txz/gameZone.jsp?gameName=` | 同上，带游戏名 |
| `sy.xunbao178.com/purchase.gsp?method=list&s_top=1&aid=<aid>` | **完美系商品列表页** |
| `www.xunbao178.com/zx/`（诛仙）<br>`www.xunbao178.com/wmgj/`（完美国际） | **502 —— 后端挂了** |

---

## 三、可用接口（全部实测）

### 3.1 完美系游戏列表 ★

```
GET http://sy.xunbao178.com/purchase.gsp
    ?method=gamesNew&alpha=&query=&type=-1&gameCompanyId=0
```

**`type=-1` 返回 24 个完美系游戏**：

```
诛仙2(40151)      梦幻新诛仙(40137)   完美国际(10)      我的起源(40145)
新武林外传(9)      诛仙(11,40149)     完美世界(1)       新笑傲江湖(40127,40128)
梦幻诛仙(15)       完美诸神(40141)     黑猫奇闻社(40143)  新神魔大陆(40131,40132)
笑傲江湖(23)       武林外传(40123,40124) 神雕侠侣2(40125,40126)
神鬼传奇(18)       神鬼世界(25)       旧日传说(40133,40134) 神魔大陆(19)
赤壁(12)          战神遗迹(40135,40136) 倚天屠龙记(40101,40102)
射雕英雄传(40097,40098) 神雕侠侣(40003,40004)
```

**`type` 取值**：`-1`=全部(24)　`2`=端游(10)　`0`=手游(15)

**★ `诛仙` 的 `gameId='11,40149'`，与 exe 里的 `gameId=11` 完全对上。**

### 3.2 完美系商品列表

```
GET http://sy.xunbao178.com/purchase.gsp?method=comms&aid=<aid>&aidName=<名字>
```
返回 **HTML 片段**（不是 JSON），内容含 `<input id="hiddenCount" value="N">`

### 3.3 匿名 token

```
GET http://hot.xunbao178.com/login.gsp?method=getLoginToken
→ {"msg":"未登录","code":401}      ← 需要登录，不是匿名的
```

### 3.4 商品列表（腾讯系，对比用）

```
POST http://xlyqq.xunbao88.com.cn/scriptmanager/gv.do?method=listComm
data: { fromType:"XUNBAOMALL", pcFlag:1, commSellerType, gamename,
        gameos, typeId, commType, gameserver, sort, platformType,
        startPrice, endPrice, commPropertyType, pushType, keySearch,
        pageNo, pageSize, token }
```

### 3.5 区服列表（腾讯系）

```
GET http://xlyqq.xunbao88.com.cn/scriptmanager/gv.do
    ?method=listCommTypeV2&gamename=<游戏名>
```
- `gamename=王者荣耀` → `['全部','安卓QQ','安卓微信','苹果QQ','苹果微信','全部安卓','全部苹果']`
- `gamename=诛仙/完美国际/完美世界/...` → **全部返回默认值** `['全部','全部安卓','全部苹果']`

**→ 完美系游戏不在这个接口的库里。**

---

## 四、关键结论

### 4.1 寻宝178 的"查区"是**商品维度**的，不是**账号维度**的

| | 寻宝178 能做的 | 我们需要的 |
|:--|:--|:--|
| 查询对象 | **商品（在售的号）** | **账号** |
| 输入 | 游戏 + 等级/战力/VIP 筛选 | 账号 + 密码 |
| 输出 | 有哪些号在卖、等级多少 | 该账号在哪些区服有角色 |
| 区服筛选 | 腾讯系有；**完美系没有**（`sells.js` 里无区服字段） | — |

**完美系商品列表页的筛选维度是「角色等级 / VIP等级 / 战力」，没有区服筛选。**

### 4.2 它**不能替代** `getRoleListByServerJsonp`

那个接口才是"按账号查该账号在哪些区服有角色"：
```
https://event.games.wanmei.com/server/list/getRoleListByServerJsonp
    ?gameId=11&server=<区服ID>&key=<key>
```
寻宝178 没有对应的公开接口。

### 4.3 但仍有三个价值

1. **官方合作背书** —— 这类账号数据交易是官方允许的
2. **区服表可交叉验证** —— 商品数据里的区服名可与 exe 里那 53 个对照
3. **市场行情参考** —— 如果需要"某区服有哪些号在卖、什么等级"，这个站现成可用

---

## 五、需要向用户确认

> **"这个地方可以查区" —— 具体是指什么？**

可能的意思：

| # | 可能的意思 | 对应做法 |
|:--|:--|:--|
| A | 能在页面上**看到区服列表** | 已拿到（exe 里 53 个 + 寻宝的游戏列表） |
| B | 能**按账号**查出该账号在哪些区服有角色 | 寻宝178 **没有**公开接口；需登录后走发布商品流程 |
| C | 能**按区服**看有哪些号在卖、等级多少 | 腾讯系有；完美系目前只有等级筛选 |
| D | 有人告诉你"从这里能拿到区服数据" | 需要问清楚具体页面/操作路径 |

**如果是 B（按账号查区）**：那需要**登录寻宝账号**，走"发布商品"流程 —— 发布时系统会校验账号并显示其区服/角色。这条链路需要账号才能验证。

**如果是 C**：完美系的区服筛选可能需要先登录，或者用别的参数。
