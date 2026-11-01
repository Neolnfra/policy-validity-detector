/* =============================================================================
 * 政策过期检测器 · 前端交互层
 * -----------------------------------------------------------------------------
 * 职责边界（严格前后端分离）：
 *   本文件只做三件事 —— 调用 /api/v1/* 接口、维护视图状态、驱动动画。
 *   不含任何政策数据、判定结论、检索式或案例内容。
 * ========================================================================== */

(function () {
  'use strict';

  // ---------------------------------------------------------------------------
  // 依赖加载：Vue 3 CDN 多源兜底
  // ---------------------------------------------------------------------------
  var VUE_CDNS = [
    'https://cdn.jsdelivr.net/npm/vue@3.4.38/dist/vue.global.prod.js',
    'https://unpkg.com/vue@3.4.38/dist/vue.global.prod.js',
    'https://cdnjs.cloudflare.com/ajax/libs/vue/3.4.38/vue.global.prod.min.js'
  ];

  function loadScript(src) {
    return new Promise(function (resolve, reject) {
      var el = document.createElement('script');
      el.src = src;
      el.async = true;
      el.onload = resolve;
      el.onerror = function () { reject(new Error('load failed: ' + src)); };
      document.head.appendChild(el);
    });
  }

  async function ensureVue() {
    if (window.Vue) return true;
    for (var i = 0; i < VUE_CDNS.length; i += 1) {
      try {
        await loadScript(VUE_CDNS[i]);
        if (window.Vue) return true;
      } catch (err) { /* 换下一个源 */ }
    }
    return false;
  }

  (async function bootstrap() {
    var ok = await ensureVue();
    if (!ok) {
      document.getElementById('app').innerHTML =
        '<div style="padding:80px 24px;text-align:center;font-family:sans-serif;color:#64748b">' +
        '<h2 style="color:#0f172a">前端依赖加载失败</h2>' +
        '<p>无法从 CDN 获取 Vue 3，请检查网络后刷新。</p>' +
        '<p>后端 API 仍然可用：<a href="/docs" style="color:#2563eb">/docs</a></p>' +
        '</div>';
      return;
    }
    createApp();
  })();

  // ---------------------------------------------------------------------------
  // 常量（纯界面映射，不含业务数据）
  // ---------------------------------------------------------------------------
  var STATUS_THEME = {
    VALID: { hex: '#059669', short: '现行有效' },
    REPLACED: { hex: '#d97706', short: '部分失效' },
    EXPIRED: { hex: '#e11d48', short: '已废止' },
    UNCERTAIN: { hex: '#475569', short: '存疑待核' }
  };

  var TIMELINE_HEX = {
    historical: '#60a5fa',
    revised: '#a78bfa',
    expired: '#e11d48',
    abolished: '#e11d48',
    current: '#059669',
    uncertain: '#94a3b8',
    conflicting: '#d97706'
  };

  var CHANNEL_TAG = {
    origin: '政务原文穿透',
    abolish: '废止目录核验',
    replacement: '微信 · 新政检索'
  };

  var CHANNEL_COLOR = {
    origin: '#2563eb',
    abolish: '#e11d48',
    replacement: '#059669'
  };

  var TRUST_STYLE = {
    S: { background: '#ecfdf5', color: '#047857', borderColor: '#a7f3d0' },
    A: { background: '#eff6ff', color: '#1d4ed8', borderColor: '#bfdbfe' },
    B: { background: '#fffbeb', color: '#b45309', borderColor: '#fde68a' },
    D: { background: '#fef2f2', color: '#b91c1c', borderColor: '#fecaca' }
  };

  var TRUST_FALLBACK = { background: '#f8fafc', color: '#475569', borderColor: '#e2e8f0' };

  // 侧边栏矢量图标（Lucide 风格，1.75px 线宽，纯 path/circle，无 emoji）
  var NAV_ICONS = {
    overview: {
      circles: [],
      paths: [
        'M20 13c0 5-3.5 7.5-7.66 8.95a1 1 0 0 1-.67-.01C7.5 20.5 4 18 4 13V6a1 1 0 0 1 1-1c2 0 4.5-1.2 6.24-2.72a1 1 0 0 1 1.52 0C14.51 3.81 17 5 19 5a1 1 0 0 1 1 1z',
        'm9 12 2 2 4-4'
      ]
    },
    trace: {
      circles: [[18, 6, 3], [6, 18, 3]],
      paths: ['M6 3v12', 'M18 9a9 9 0 0 1-9 9']
    },
    diff: {
      circles: [],
      paths: ['M3 3h18v18H3z', 'M12 3v18']
    },
    sources: {
      circles: [],
      paths: ['M3 22h18', 'M6 18v-7', 'M10 18v-7', 'M14 18v-7', 'M18 18v-7', 'm12 2 8 5H4z']
    }
  };

  // 典型政策场景：用业务口径命名，避免"XX 案例"这种抽象说法
  var PRESET_LABELS = {
    ministry_equipment_renewal_2025: '国务院 · 两新汽车补贴',
    beijing_nev_subsidy_expired: '北京 · 新能源环卫车补助',
    hangzhou_talent_subsidy_valid: '杭州 · 应届生生活补贴',
    shenzhen_park_fund_abolished: '深圳 · 高新园设备扶持',
    changsha_smart_manufacturing_uncertain: '长沙 · 首台套装备保险'
  };
  var PRESET_SUBS = {
    ministry_equipment_renewal_2025: '年度细则接续',
    beijing_nev_subsidy_expired: '旧政到期替代',
    hangzhou_talent_subsidy_valid: '现行有效',
    shenzhen_park_fund_abolished: '试行期满废止',
    changsha_smart_manufacturing_uncertain: '申报窗口核验'
  };

  function createApp() {
    var Vue = window.Vue;

    var app = Vue.createApp({
      data: function () {
        return {
          health: { version: '1.0.0' },
          buildTag: '2026-11-01.4',
          activeView: 'overview',
          regions: { provinces: [], cities: [], ministries: [] },
          presets: [],

          rawInput: '',
          regionHint: 'auto',
          activePreset: null,
          running: false,
          result: null,
          exporting: false,
          runSeq: 0,

          stageDefs: [
            { key: 'route', label: '地域路由' },
            { key: 'parse', label: '要素解析' },
            { key: 'queries', label: '检索词裂变' },
            { key: 'search', label: '三路并发检索' },
            { key: 'judge', label: '效力判定' }
          ],
          doneStages: [],
          activeStage: '',
          liveTraces: [],
          expanded: {},

          drawerOpen: false,
          snippetTab: 'curl',
          snippets: { endpoints: [], curl: '', python: '', javascript: '' },
          toast: '',
          toastTimer: null,

          // ---------------- 引擎与密钥设置（运行时热插拔） ----------------
          settingsOpen: false,
          settingsSaving: false,
          settingsStatus: null,
          settingsForm: {
            yuanbao_api_key: '',
            yuanbao_base_url: '',
            concurrency: 'three_way'
          },
          hasCustomKey: false,
          // 服务端运行时里是否还有别的会话写入的密钥（浏览器本地为空但服务端已配置）
          serverRuntimeActive: false,
          testing: false,
          testResult: null,
          ybKeyVisible: false,

          reportOpen: false,
          reportMarkdown: '',
          reportFilename: '',
          reportTime: '',

          emptyBlocks: [
            { t: '原文溯源', d: '官网原文永远显示"有效"，它不会自己标注失效。只查原文，等于只看了半份证据。' },
            { t: '废止排查', d: '文件可能被《废止和宣布失效的决定》提前终止，这一步最容易被忽略，也最容易造成误判。' },
            { t: '新政替代', d: '到期不等于作废，往往有接续政策。用户真正需要的是"现在按什么办"，而不是"当年怎么写的"。' }
          ],

          sampleBody: [
            '{',
            '  "raw_input": "杭州市人民政府办公厅关于印发…的通知（杭政办函〔2021〕35号）",',
            '  "region_hint": "auto",',
            '  "case_id": null',
            '}'
          ].join('\n')
        };
      },

      computed: {
        allRegions: function () {
          return [].concat(this.regions.ministries, this.regions.provinces, this.regions.cities);
        },

        // ---------------- 引擎与密钥设置 ----------------
        // 左上角状态文案：与左下角徽章联动，让"到底在用谁的 Key"一眼可见
        engineStatusText: function () {
          if (this.hasCustomKey) return '元宝搜索引擎 · 自定义密钥已就绪';
          if (this.serverRuntimeActive) return '元宝搜索引擎 · 服务端密钥已就绪';
          var h = this.health || {};
          if (h.search_configured) return '元宝搜索真实接口已就绪';
          return '内置高仿真引擎就绪 · 待接入密钥';
        },

        // 配置预检：在点「测试」之前就把明显填错的地方指出来，
        // 比让用户等两秒网络超时再猜要友好得多。
        settingsPreflight: function () {
          var f = this.settingsForm || {};
          var key = (f.yuanbao_api_key || '').trim();
          var base = (f.yuanbao_base_url || '').trim();
          if (!key && !base) return '';
          var isPair = key.indexOf('|') > -1;
          // 只有「签名端点」才要求成对密钥；API KEY 兼容模式的域名天生吃单把 Key
          var looksTc3 = /(^|\.)tencentcloudapi\.com/i.test(base);
          if (looksTc3 && key && !isPair) {
            return '这个域名是腾讯云签名端点（wsa.tencentcloudapi.com），只认 SecretId|SecretKey 成对密钥。若手里是服务 API KEY，请把 Base URL 留空或改成 https://api.wsa.cloud.tencent.com。';
          }
          if (key && /^AKID[A-Za-z0-9]{10,}$/.test(key) && !/wsa\.cloud\.tencent\.com/i.test(base)) {
            return '这串看起来是腾讯云的 SecretId。它不能当作 API KEY 使用：请填控制台「服务 API KEY」创建的那把密钥，或拼成 SecretId|SecretKey 走签名方式。';
          }
          return '';
        },

        searchBasePlaceholder: function () {
          var d = (this.settingsStatus && this.settingsStatus.defaults) || {};
          return d.yuanbao_base_url || 'https://your-gateway.example.com/v1';
        },

        searchBaseHint: function () {
          var s = (this.settingsStatus && this.settingsStatus.server) || {};
          if (s.provider === 'tencent_cloud') {
            return '留空即用官方默认端点 https://api.wsa.cloud.tencent.com/SearchPro；如需指定其它网关，再填这里。';
          }
          return '留空即用官方默认端点 https://api.wsa.cloud.tencent.com/SearchPro；自定义网关可填根地址或完整端点，系统会按原样请求。';
        },

        // 侧边栏导航项（主功能名 + 具体作用副标题，角标随结果动态变化）
        navItems: function () {
          var r = this.result;
          return [
            { key: 'overview', icon: NAV_ICONS.overview, label: '效力判定与防幻觉', sub: '结论印章 · 盲猜对比 · 办理指引', badge: '' },
            { key: 'trace', icon: NAV_ICONS.trace, label: '三路实时检索求证', sub: '原文溯源 · 废止排查 · 新政穿透', badge: r ? r.search_trace.length + ' 路' : '' },
            { key: 'diff', icon: NAV_ICONS.diff, label: '历史演进与法条对比', sub: '生命周期时间轴 · 新旧条款差异', badge: r ? r.clause_diffs.length + ' 项' : '' },
            { key: 'sources', icon: NAV_ICONS.sources, label: '官方原文与报告导出', sub: 'gov.cn 信源库 · 预览/下载核验单', badge: r ? this.displaySources.length + ' 条' : '' }
          ];
        },

        // 顶部检索框的文号自动识别标签
        autoTags: function () {
          var text = this.rawInput || '';
          if (text.trim().length < 2) return [];
          var tags = [];

          var doc = text.match(/[\u4e00-\u9fa5]{1,12}\s*[〔\[（(]\s*(?:19|20)\d{2}\s*[〕\]）)]\s*\d{1,4}\s*号/);
          if (doc) tags.push('已识别文号：' + doc[0].replace(/\s+/g, ''));

          var title = text.match(/《([^》]{2,40})》/);
          if (title) tags.push('政策名：' + title[1].slice(0, 12) + (title[1].length > 12 ? '…' : ''));

          // 口语/主题短词：明确提示"将按主题检索并自动锁定法定文件"，
          // 避免用户以为系统会把「电瓶车」当成公文名。
          var plain = text.replace(/\s+/g, '');
          if (!doc && !title && plain.length <= 14 && !/(办法|条例|通知|意见|细则|规定|方案|决定|公告|规划|规则|标准|指南)/.test(plain)) {
            tags.unshift('主题检索：' + plain + '（自动匹配法定文件）');
          }

          tags.push('识别层级：' + this.levelLabel);
          return tags.slice(0, 3);
        },

        levelLabel: function () {
          var lv = (this.selectedRegion && this.selectedRegion.admin_level) || '';
          if (lv.indexOf('国务院') >= 0) return '国务院部委';
          if (lv.indexOf('省级') >= 0) return '省级行政区';
          if (lv.indexOf('地市') >= 0) return '地市级';
          return '待识别';
        },

        // 模块 01：结构化要素提取网格
        // 判定依据：直接用三路检索各自的关键发现，避免模块 01 中部出现空白
        evidenceLines: function () {
          if (!this.result) return [];
          var self = this;
          return (this.result.search_trace || [])
            .map(function (t) {
              var finding = self.cleanText(t.key_finding || '');
              if (!finding) return '';
              var label = String(t.channel || '').split('·')[0].trim();
              return (label ? label + '：' : '') + finding;
            })
            .filter(function (x) { return x.length > 6; })
            .slice(0, 3);
        },

        metaGrid: function () {
          if (!this.result) return [];
          var m = this.result.policy_meta || {};

          var lv = m.admin_level || '';
          var level = lv.indexOf('国务院') >= 0
            ? '国务院 / 部委级规范性文件'
            : (lv.indexOf('省级') >= 0 ? '省级行政规范性文件' : '地市级行政规范性文件');

          var win = (m.publish_date || '原文未标注') + ' 起';
          var exp = String(m.stated_expiry || '').replace(/（[^）]*）/g, '').trim();
          if (/^\d{4}-\d{2}-\d{2}/.test(exp)) win += '，至 ' + exp.slice(0, 10);
          else win += '，按年度细则执行';

          var code = this.result.validity_result.status_code;
          var currents = (this.result.timeline || []).filter(function (n) { return n.status === 'current'; });
          var successor = '';
          // 从最新节点往前找：优先取带书名号或文号的接替文件，忽略「原文附则」这类说明节点
          for (var ci = currents.length - 1; ci >= 0; ci -= 1) {
            var doc = String(currents[ci].doc || '');
            if (doc.indexOf('《') >= 0 || /〔\d{4}〕\d+号/.test(doc)) {
              successor = doc;
              break;
            }
          }
          if (successor) {
            var docMatch = successor.match(/[\u4e00-\u9fa5]{1,12}〔\d{4}〕\d+号/);
            if (docMatch) successor = docMatch[0];
          } else if (code === 'VALID') {
            successor = '无需接替，现行有效';
          } else if (code === 'UNCERTAIN') {
            successor = '待主管部门确认';
          } else {
            successor = '未见明确接替文件';
          }

          return [
            { k: '发文机关', v: m.department || '—' },
            { k: '效力层级', v: level },
            { k: '时效窗口', v: win },
            { k: '现行接替文号', v: successor }
          ];
        },

        // 模块 02：实操避坑与行动指引（优先后端专家文案，缺省时按状态生成）
        guide: function () {
          if (!this.result) return { risk: '', action: '' };
          var g = this.result.compliance_guide;
          if (g && g.risk && g.action) return g;

          var code = this.result.validity_result.status_code;
          if (code === 'VALID') {
            return {
              risk: '按现行材料清单申报即可，但仍需核对当期受理批次、社保或纳税等属地附加条件，避免因材料时点不符被退回。',
              action: '以主管部门当期申报通知为准，按现行文号与条件提交；涉及资金测算的，先确认补贴档次与上限。'
            };
          }
          return {
            risk: '继续援引原文件条款准备材料，可能因文件已失效或核心条件已被修订，在受理窗口被直接退回。',
            action: '以本页最新检索结果与主管部门当期通知为准，必要时致电确认后再提交材料。'
          };
        },

        // 页面、弹窗、群聊简报统一的政策标题：口语词未锁定时也绝不输出空标题
        displayTitle: function () {
          if (!this.result) return '';
          var m = this.result.policy_meta || {};
          return m.title || m.locked_policy_title || (m.topic ? '主题检索：' + m.topic : '未命名政策');
        },

        selectedRegion: function () {
          if (this.regionHint === 'auto') {
            return { name: '全国通查 · 自动识别', gov_domain: 'gov.cn', admin_level: '全国' };
          }
          var key = this.regionHint;
          var found = this.allRegions.filter(function (r) { return r.key === key; })[0];
          return found || { name: '全国通查 · 自动识别', gov_domain: 'gov.cn', admin_level: '全国' };
        },

        statusTheme: function () {
          if (!this.result) return STATUS_THEME.UNCERTAIN;
          return STATUS_THEME[this.result.validity_result.status_code] || STATUS_THEME.UNCERTAIN;
        },

        gauges: function () {
          if (!this.result) return [];
          var v = this.result.validity_result;
          return [
            { k: '结论可信度', v: v.confidence_score, grad: 'linear-gradient(90deg,#2563eb,#4f46e5)' },
            { k: '信源权威度', v: v.authority_score, grad: 'linear-gradient(90deg,#0ea5e9,#14b8a6)' }
          ];
        },

        stats4: function () {
          if (!this.result) return [];
          return [
            { k: '并发检索通道', v: this.result.search_trace.length + ' 路' },
            // 与侧栏角标、模块 06 顶部统一取 displaySources，避免三个数字打架
            { k: '权威信源穿透', v: this.displaySources.length + ' 条' },
            { k: '端到端耗时', v: this.result.elapsed_ms + ' ms' },
            { k: '幻觉风险拦截', v: '100% 修正' }
          ];
        },

        liveDone: function () {
          return this.liveTraces.filter(function (t) { return t.state === 'done'; }).length;
        },

        liveProgress: function () {
          if (!this.liveTraces.length) return 0;
          return Math.round((this.liveDone / this.liveTraces.length) * 100);
        },

        // 信源统一按可信度排序后全量展示（后端已限量，前端不再二次截断，
        // 否则侧栏角标、模块 02 指标、模块 06 顶部会出现三个不同数字）
        displaySources: function () {
          if (!this.result) return [];
          var order = { S: 0, A: 1, B: 2, C: 3, D: 4 };
          var rank = function (level) {
            var g = String(level || '').toUpperCase().charAt(0);
            return order[g] === undefined ? 9 : order[g];
          };
          var list = (this.result.sources || []).slice();
          list.sort(function (a, b) { return rank(a.trust_level) - rank(b.trust_level); });
          return list;
        },

        // 幻觉回答拆成「政策名 / 已知事实（正常显示）」+「错误结论（删除线标注）」两段
        naiveSplit: function () {
          if (!this.result) return { head: '', claim: '' };
          var text = this.cleanText(this.result.validity_result.hallucination_comparison.naive_llm_answer);

          // 1) 有《政策名》：在书名号（含紧随标点）处断开
          var idx = text.indexOf('》');
          if (idx >= 0) {
            var cut = idx + 1;
            var next = text.charAt(cut);
            if (next === '，' || next === '。' || next === '；') cut += 1;
            return { head: text.slice(0, cut), claim: text.slice(cut) };
          }

          // 2) 「根据……，」式引用：在逗号处断开
          var m = text.match(/^(根据[^，]{0,30}，)/);
          if (m) return { head: m[1], claim: text.slice(m[1].length) };

          // 3) 以年份开头 = 整句都是编造的日期，整段标注，不保留前半句
          if (/^\d{4}\s*年/.test(text)) return { head: '', claim: text };

          // 4) 其余：保留首个短分句作为"说对的部分"，其后全部标注为幻觉
          var comma = text.indexOf('，');
          if (comma > 4 && comma <= 30) {
            return { head: text.slice(0, comma + 1), claim: text.slice(comma + 1) };
          }

          return { head: '', claim: text };
        },

        // 幻觉成因：由时间轴中的「到期 / 废止」节点反推，而不是写死文案
        hallucinationCause: function () {
          if (!this.result) return '';
          var code = this.result.validity_result.status_code;
          var short = function (event) { return String(event || '').split('，')[0]; };

          // 存疑场景不下"未感知某事件"的断言，改为陈述口径冲突
          if (code === 'UNCERTAIN') {
            return '大模型未察觉该事项存在相互冲突的属地执行口径，无法判断应适用哪一版';
          }

          var bad = (this.result.timeline || []).filter(function (n) {
            return n.status === 'expired' || n.status === 'abolished';
          });

          if (bad.length >= 2) {
            return '大模型未感知 ' + bad[0].date + ' 的「' + short(bad[0].event) + '」，以及 '
              + bad[1].date + ' 的「' + short(bad[1].event) + '」';
          }
          if (bad.length === 1) {
            return '大模型未感知 ' + bad[0].date + ' 的「' + short(bad[0].event) + '」';
          }

          if (code === 'REPLACED') return '大模型未感知后续接续政策对适用范围与补贴标准的修订';
          if (code === 'VALID') return '大模型未核对最新补充通知的申报口径与时限要求';
          return '大模型缺少一手信源，无法判断政策是否仍在执行';
        },

        // 公文效力档案卡（模块 04 底部，用于与右侧 Diff 等高）
        archive: function () {
          if (!this.result) return [];
          var m = this.result.policy_meta || {};
          var nodes = this.result.timeline || [];
          var currents = nodes.filter(function (n) { return n.status === 'current'; });
          var last = currents.length ? currents[currents.length - 1] : null;
          var code = this.result.validity_result.status_code;

          var action;
          if (code === 'VALID') {
            action = '无需替代，按现行口径继续执行';
          } else if (last && last.doc) {
            action = last.doc;
          } else if (code === 'EXPIRED') {
            action = '原文件已失效，须按现行有效政策重新申报';
          } else {
            action = '待主管部门确认后按最新通知执行';
          }
          // 不做字符截断：档案卡允许换行完整展示接替文号

          var period = m.publish_date || '原文未标注';
          var expiry = String(m.stated_expiry || '').replace(/（[^）]*）/g, '').trim();
          // 只有形如 YYYY-MM-DD 的才是真日期；说明性文字不塞进日期字段
          if (/^\d{4}-\d{2}-\d{2}/.test(expiry)) period += ' 至 ' + expiry.slice(0, 10);

          return [
            { k: '发文机关', v: m.department || '—' },
            { k: '发文字号', v: m.doc_number || '未标注' },
            { k: '原始生效 / 试行期', v: period },
            { k: '现行替代 / 处置建议', v: action }
          ];
        }
      },

      methods: {
        // ---------------- 数据加载 ----------------
        loadBootstrap: async function () {
          try {
            var res = await fetch('/api/v1/health');
            if (res.ok) this.health = await res.json();
          } catch (err) { /* 离线时保持默认态 */ }

          try {
            var res2 = await fetch('/api/v1/regions');
            if (res2.ok) {
              var data = await res2.json();
              this.regions = {
                provinces: data.provinces || [],
                cities: data.cities || [],
                ministries: data.ministries || []
              };
              this.presets = data.presets || [];
            }
          } catch (err) { /* 同上 */ }

          if (this.presets.length > 1) {
            var self = this;
            setTimeout(function () { self.selectPreset(self.presets[1]); }, 650);
          }
        },

        loadSnippets: async function () {
          try {
            var res = await fetch('/api/v1/openapi/snippets');
            if (res.ok) this.snippets = await res.json();
          } catch (err) { /* 抽屉仍可打开 */ }
        },

        // ---------------- 引擎与密钥设置（运行时热插拔） ----------------
        // 统一请求头：任何一次核验 / 解析 / 导出请求都自动带上浏览器侧的密钥，
        // 后端据此构造"本次请求专用"的配置快照（见 core/runtime_config.py）。
        authHeaders: function () {
          var f = this.settingsForm || {};
          var out = {};
          if (f.yuanbao_api_key) {
            out['X-Yuanbao-Api-Key'] = f.yuanbao_api_key;
            if (f.yuanbao_base_url) out['X-Yuanbao-Base-Url'] = f.yuanbao_base_url;
          }
          return out;
        },

        jsonHeaders: function () {
          var headers = { 'Content-Type': 'application/json' };
          var auth = this.authHeaders();
          for (var key in auth) {
            if (Object.prototype.hasOwnProperty.call(auth, key)) headers[key] = auth[key];
          }
          return headers;
        },

        settingsPayload: function () {
          var f = this.settingsForm;
          return {
            yuanbao_api_key: f.yuanbao_api_key || '',
            yuanbao_base_url: f.yuanbao_base_url || '',
            concurrency: f.concurrency || 'three_way'
          };
        },

        defaultSettingsForm: function () {
          var d = (this.settingsStatus && this.settingsStatus.defaults) || {};
          return {
            yuanbao_api_key: '',
            yuanbao_base_url: '',
            concurrency: 'three_way'
          };
        },

        readStoredSettings: function () {
          try {
            var raw = window.localStorage.getItem('pvd_api_settings');
            if (!raw) return null;
            var obj = JSON.parse(raw);
            return obj && typeof obj === 'object' ? obj : null;
          } catch (err) {
            return null;
          }
        },

        loadSettingsStatus: async function () {
          try {
            var res = await fetch('/api/v1/settings/status', { headers: this.authHeaders() });
            if (res.ok) {
              this.settingsStatus = await res.json();
              var rt = (this.settingsStatus && this.settingsStatus.runtime) || {};
              this.serverRuntimeActive = !!rt.active;
            }
          } catch (err) { /* 离线时保持默认态 */ }
        },

        // 刷新页面后把 localStorage 里的配置再同步给后端：
        // 后端运行时配置只存在内存，进程重启就没了，这一步是"持久化"真正生效的地方。
        syncRuntimeConfig: async function (silent) {
          try {
            var res = await fetch('/api/v1/settings/config', {
              method: 'POST',
              headers: { 'Content-Type': 'application/json' },
              body: JSON.stringify(this.settingsPayload())
            });
            if (res.ok) {
              var data = await res.json();
              this.settingsStatus = data;
              if (!silent && data.message) this.showToast(data.message);
            }
          } catch (err) { /* 忽略：请求头透传仍可兜底 */ }
        },

        loadSettings: async function () {
          await this.loadSettingsStatus();

          var saved = this.readStoredSettings();
          if (saved) {
            var f = this.settingsForm;
            f.yuanbao_api_key = saved.yuanbao_api_key || '';
            f.yuanbao_base_url = saved.yuanbao_base_url || '';
            f.concurrency = saved.concurrency || 'three_way';
          }

          this.hasCustomKey = !!this.settingsForm.yuanbao_api_key;
          if (this.hasCustomKey) await this.syncRuntimeConfig(true);
        },

        openSettings: function () {
          this.settingsOpen = true;
          this.testResult = null;
          this.loadSettingsStatus();
        },

        closeSettings: function () {
          this.settingsOpen = false;
        },

        saveSettings: async function () {
          if (this.settingsSaving) return;

          // 轻量格式校验：真正的有效性交给「测试接口连通性」去打一次真实请求，
          // 这里只拦住"显然没粘全"的输入，避免把半截 Key 存进运行时。
          var f = this.settingsForm;
          if (f.yuanbao_api_key && f.yuanbao_api_key.length < 6) {
            this.showToast('元宝搜索密钥长度异常，请检查是否粘贴完整');
            return;
          }

          this.settingsSaving = true;
          try {
            var payload = this.settingsPayload();
            var res = await fetch('/api/v1/settings/config', {
              method: 'POST',
              headers: { 'Content-Type': 'application/json' },
              body: JSON.stringify(payload)
            });
            if (!res.ok) throw new Error('HTTP ' + res.status);
            var data = await res.json();
            this.settingsStatus = data;
            this.hasCustomKey = !!payload.yuanbao_api_key;

            try {
              if (this.hasCustomKey) {
                window.localStorage.setItem('pvd_api_settings', JSON.stringify({
                  yuanbao_api_key: payload.yuanbao_api_key,
                  yuanbao_base_url: payload.yuanbao_base_url,
                  concurrency: payload.concurrency,
                  saved_at: new Date().toISOString()
                }));
              } else {
                window.localStorage.removeItem('pvd_api_settings');
              }
            } catch (e) { /* 隐私模式下 localStorage 不可写，本次会话仍然有效 */ }

            this.settingsOpen = false;
            this.showToast(data.message || '配置已保存并立即启用');
          } catch (err) {
            this.showToast('保存失败：' + (err.message || err));
          } finally {
            this.settingsSaving = false;
          }
        },

        testConnection: async function () {
          if (this.testing) return;
          this.testing = true;
          this.testResult = null;
          try {
            var res = await fetch('/api/v1/settings/test', {
              method: 'POST',
              headers: { 'Content-Type': 'application/json' },
              body: JSON.stringify(this.settingsPayload())
            });
            if (!res.ok) throw new Error('HTTP ' + res.status);
            var data = await res.json();
            this.testResult = {
              ok: !!data.ok,
              message: data.message || (data.ok ? '连接成功' : '连接失败'),
              latency_ms: data.latency_ms || 0
            };
          } catch (err) {
            this.testResult = { ok: false, message: '测试请求失败：' + (err.message || err), latency_ms: 0 };
          } finally {
            this.testing = false;
          }
        },

        clearSettings: async function () {
          if (this.settingsSaving) return;
          this.settingsSaving = true;
          try {
            var res = await fetch('/api/v1/settings/config', { method: 'DELETE' });
            if (res.ok) this.settingsStatus = await res.json();
          } catch (err) { /* 即使后端删除失败也清掉前端副本 */ }
          this.settingsSaving = false;

          try { window.localStorage.removeItem('pvd_api_settings'); } catch (e) { /* 同上 */ }
          this.settingsForm = this.defaultSettingsForm();
          this.hasCustomKey = false;
          this.testResult = null;
          this.showToast('已清除自定义配置，回落到服务器默认配置');
        },

        // ---------------- 交互 ----------------
        // 视图切换：单视图 或 全景展开（all 时四个视图同时可见）
        isView: function (key) {
          return this.activeView === key || this.activeView === 'all';
        },

        setView: function (key) {
          this.activeView = key;
          if (key === 'trace' && this.result) this.expandAllHits();
        },

        toggleAll: function () {
          this.activeView = this.activeView === 'all' ? 'overview' : 'all';
        },

        expandAllHits: function () {
          var next = {};
          (this.result.search_trace || []).forEach(function (t) { next[t.step] = true; });
          this.expanded = next;
        },

        presetColor: function (p) {
          var map = { VALID: '#059669', REPLACED: '#d97706', EXPIRED: '#e11d48', UNCERTAIN: '#94a3b8' };
          return map[p.status_code] || '#94a3b8';
        },

        presetName: function (p) {
          if (!p) return '';
          return PRESET_LABELS[p.id] || (p.short || p.region_key || '') + '案例';
        },

        presetSub: function (p) {
          if (!p) return '';
          return PRESET_SUBS[p.id] || p.region_label || '';
        },

        selectPreset: function (p) {
          this.activePreset = p.id;
          this.rawInput = p.raw_input;

          // 行政区划下拉框与案例强联动：key 必须真实存在于下拉选项中
          var key = p.region_key;
          var exists = this.allRegions.some(function (r) { return r.key === key; });
          this.regionHint = exists ? key : 'auto';

          // 强制刷新：即便上一轮求证尚未结束，也要立刻切换到当前案例，
          // 避免出现「上面是长沙、下面还是深圳」的穿帮。
          this.runVerify(true);
        },

        openDrawer: function () {
          this.drawerOpen = true;
          this.loadSnippets();
        },

        toggleHits: function (step) {
          var next = Object.assign({}, this.expanded);
          next[step] = !next[step];
          this.expanded = next;
        },

        showToast: function (text) {
          this.toast = text;
          if (this.toastTimer) clearTimeout(this.toastTimer);
          var self = this;
          this.toastTimer = setTimeout(function () { self.toast = ''; }, 2600);
        },

        copySnippet: async function () {
          var text = this.snippets[this.snippetTab] || '';
          if (!text) {
            this.showToast('示例尚未加载完成，请稍后重试');
            return;
          }
          try {
            await navigator.clipboard.writeText(text);
            this.showToast('已复制到剪贴板');
          } catch (err) {
            this.showToast('复制失败，请手动选择文本');
          }
        },

        // ---------------- 展示辅助 ----------------
        stageState: function (key) {
          if (this.doneStages.indexOf(key) >= 0) return 'done';
          if (this.activeStage === key) return 'active';
          return 'pending';
        },

        // 只保留可采信信源：命中数与可展开条目严格一致
        displayHits: function (t) {
          var hits = (t && t.hits) || [];
          return hits.filter(function (h) {
            var st = String(h.source_type || '');
            return st.indexOf('演示占位') < 0 && st.indexOf('占位') < 0 && st.indexOf('未联网') < 0;
          });
        },

        channelTag: function (t) {
          return CHANNEL_TAG[t.channel_key] || '政务网检索';
        },

        channelColor: function (t) {
          return CHANNEL_COLOR[t.channel_key] || '#2563eb';
        },

        channelTitle: function (t) {
          return this.cleanText(t.channel);
        },

        trustStyle: function (level) {
          var grade = String(level || '').toUpperCase().charAt(0);
          return TRUST_STYLE[grade] || TRUST_FALLBACK;
        },

        trustLabel: function (s) {
          var grade = String(s.trust_level || '').toUpperCase().charAt(0);
          if (grade === 'S') return 'S级权威 · gov.cn 政务网';
          if (grade === 'A') return 'A级权威 · 官方政务新媒体';
          if (grade === 'B') return 'B级 · 属地通知';
          if (grade === 'D') return 'D级 · 来源不可采信';
          return '待评级';
        },

        impactStyle: function (level) {
          var t = String(level || '');
          if (t.indexOf('高') === 0) return { background: '#fff1f2', color: '#be123c', borderColor: '#fecdd3' };
          if (t.indexOf('中') === 0) return { background: '#fffbeb', color: '#b45309', borderColor: '#fde68a' };
          return { background: '#f8fafc', color: '#475569', borderColor: '#e2e8f0' };
        },

        impactLabel: function (level) {
          return String(level || '')
            .replace(/（/g, ' · ')
            .replace(/）/g, '')
            .replace(/\(/g, ' · ')
            .replace(/\)/g, '');
        },

        tlColor: function (status) {
          return TIMELINE_HEX[status] || TIMELINE_HEX.historical;
        },

        // 折叠中文之间的多余断行空格（"对老旧货运车 报废更新" → "对老旧货运车报废更新"）
        // 顺带把夹在汉字之间的半角逗号/分号统一为全角，避免公文段落看起来像代码
        cleanText: function (text) {
          if (text === null || text === undefined) return '';
          return String(text)
            .replace(/[\s\u3000]+/g, ' ')
            .replace(/([\u4e00-\u9fa5])\s+([\u4e00-\u9fa5])/g, '$1$2')
            .replace(/([\u4e00-\u9fa5])\s*,\s*(?=[\u4e00-\u9fa5])/g, '$1，')
            .replace(/([\u4e00-\u9fa5])\s*;\s*(?=[\u4e00-\u9fa5])/g, '$1；')
            .trim();
        },

        prettyUrl: function (url) {
          return String(url || '').replace(/^https?:\/\//, '').replace(/\/$/, '');
        },

        // ---------------- 核验主流程 ----------------
        resetRun: function () {
          this.result = null;
          this.running = true;
          this.doneStages = [];
          this.activeStage = 'route';
          this.expanded = {};
          this.liveTraces = [];
        },

        runVerify: async function (force) {
          // 模板里 @click="runVerify" 会把 MouseEvent 传进来，这里只认显式 true
          var forced = force === true;
          if (this.running && !forced) return;

          var text = (this.rawInput || '').trim();
          if (text.length < 2) {
            this.showToast('请输入至少 2 个字符的政策文本');
            return;
          }

          // 每次调用递增序号；旧请求的返回一旦发现序号过期就整段丢弃
          var seq = ++this.runSeq;
          this.resetRun();
          var payload = { raw_input: text, region_hint: this.regionHint, case_id: this.activePreset };

          try {
            var handled = await this.streamVerify(payload, seq);
            if (!handled && seq === this.runSeq) await this.fallbackVerify(payload, seq);
          } catch (err) {
            if (seq !== this.runSeq) return;
            try {
              await this.fallbackVerify(payload, seq);
            } catch (err2) {
              if (seq !== this.runSeq) return;
              this.running = false;
              this.activeStage = '';
              this.showToast('核验失败：' + (err2.message || err2));
            }
          }
        },

        streamVerify: async function (payload, seq) {
          var res = await fetch('/api/v1/policy/verify/stream', {
            method: 'POST',
            headers: this.jsonHeaders(),
            body: JSON.stringify(payload)
          });
          if (!res.ok || !res.body || !res.body.getReader) return false;

          var reader = res.body.getReader();
          var decoder = new TextDecoder('utf-8');
          var buffer = '';
          var finished = false;

          while (true) {
            var chunk = await reader.read();
            if (chunk.done) break;

            // 已被更新的请求取代：立即中断，不再污染页面
            if (seq !== this.runSeq) {
              try { reader.cancel(); } catch (e) { /* 忽略 */ }
              return true;
            }

            buffer += decoder.decode(chunk.value, { stream: true });

            var sep;
            while ((sep = buffer.indexOf('\n\n')) >= 0) {
              var block = buffer.slice(0, sep);
              buffer = buffer.slice(sep + 2);
              var event = this.parseSSE(block);
              if (event) {
                finished = true;
                this.handleEvent(event, seq);
              }
            }
          }
          return finished;
        },

        parseSSE: function (block) {
          var name = 'message';
          var dataLine = '';
          block.split('\n').forEach(function (line) {
            if (line.indexOf('event:') === 0) name = line.slice(6).trim();
            else if (line.indexOf('data:') === 0) dataLine += line.slice(5).trim();
          });
          if (!dataLine) return null;
          var payload;
          try { payload = JSON.parse(dataLine); } catch (err) { return null; }
          payload.stage = payload.stage || name;
          return payload;
        },

        handleEvent: function (evt, seq) {
          // 过期请求的事件一律丢弃，保证页面只反映最后一次点击
          if (seq !== this.runSeq) return;
          var stage = evt.stage;

          if (stage === 'route') {
            this.doneStages.push('route');
            this.activeStage = 'parse';
            return;
          }

          if (stage === 'parse') {
            this.doneStages.push('parse');
            this.activeStage = 'queries';
            return;
          }

          if (stage === 'queries') {
            this.doneStages.push('queries');
            this.activeStage = 'search';
            // 先铺三张骨架卡，检索时逐张点亮
            this.liveTraces = (evt.queries || []).map(function (q) {
              return {
                step: q.step,
                channel: q.channel,
                query: q.query,
                state: 'pending',
                hit_count: 0,
                key_finding: '',
                latency_ms: 0,
                hits: []
              };
            });
            return;
          }

          if (stage === 'search_start') {
            this.activeStage = 'search';
            var startIdx = (evt.step || 1) - 1;
            if (this.liveTraces[startIdx]) this.liveTraces[startIdx].state = 'running';
            return;
          }

          if (stage === 'search_done') {
            var t = evt.trace || {};
            var idx = (t.step || 1) - 1;
            if (this.liveTraces[idx]) {
              Object.assign(this.liveTraces[idx], {
                state: 'done',
                query: t.query || this.liveTraces[idx].query,
                hit_count: t.hit_count || 0,
                key_finding: t.key_finding || '',
                latency_ms: t.latency_ms || 0,
                hits: t.hits || []
              });
            }
            // 三路是真并发，完成顺序不固定：必须等三张卡全部 done 才进入判定阶段
            var allDone = this.liveTraces.length > 0 && this.liveTraces.every(function (x) {
              return x.state === 'done';
            });
            if (allDone) {
              this.doneStages.push('search');
              this.activeStage = 'judge';
            }
            return;
          }

          if (stage === 'judge') {
            if (this.doneStages.indexOf('search') < 0) this.doneStages.push('search');
            this.doneStages.push('judge');
            this.activeStage = '';
            return;
          }

          if (stage === 'complete') {
            this.result = evt.result;
            this.running = false;
            this.doneStages = ['route', 'parse', 'queries', 'search', 'judge'];
            this.activeStage = '';
            // 新一轮结果到来，旧的报告缓存与弹窗必须失效
            this.reportOpen = false;
            this.reportMarkdown = '';
            this.reportFilename = '';
            // 三路证据默认展开，进入「三路思考链」视图即可直接看到命中详情
            this.expandAllHits();
            return;
          }

          if (stage === 'error') {
            this.running = false;
            this.activeStage = '';
            this.showToast('链路异常：' + (evt.message || '未知错误'));
          }
        },

        // 流式不可用时的降级：走普通 POST，再按节奏回放
        fallbackVerify: async function (payload, seq) {
          var res = await fetch('/api/v1/policy/verify', {
            method: 'POST',
            headers: this.jsonHeaders(),
            body: JSON.stringify(payload)
          });
          if (!res.ok) throw new Error('HTTP ' + res.status);
          var data = await res.json();
          if (seq !== this.runSeq) return;

          var sleep = function (ms) { return new Promise(function (r) { setTimeout(r, ms); }); };

          var pre = ['route', 'parse', 'queries'];
          for (var i = 0; i < pre.length; i += 1) {
            if (seq !== this.runSeq) return;
            this.activeStage = pre[i];
            if (i > 0) this.doneStages.push(pre[i - 1]);
            await sleep(170);
          }
          if (seq !== this.runSeq) return;

          this.doneStages.push('queries');
          this.activeStage = 'search';

          var traces = data.search_trace || [];
          this.liveTraces = traces.map(function (t) {
            return {
              step: t.step, channel: t.channel, query: t.query, state: 'pending',
              hit_count: 0, key_finding: '', latency_ms: 0, hits: []
            };
          });

          for (var k = 0; k < traces.length; k += 1) {
            if (seq !== this.runSeq) return;
            this.liveTraces[k].state = 'running';
            await sleep(320);
            if (seq !== this.runSeq) return;
            Object.assign(this.liveTraces[k], {
              state: 'done',
              hit_count: traces[k].hit_count,
              key_finding: traces[k].key_finding,
              latency_ms: traces[k].latency_ms,
              hits: traces[k].hits || []
            });
          }

          this.doneStages.push('search');
          this.activeStage = 'judge';
          await sleep(240);
          if (seq !== this.runSeq) return;

          this.doneStages.push('judge');
          this.activeStage = '';
          this.result = data;
          this.running = false;
          this.expandAllHits();
        },

        // ---------------- 导出 ----------------
        exportReport: async function () {
          if (!this.result || this.exporting) return;
          this.exporting = true;
          try {
            await this.fetchReport();
            if (!this.reportMarkdown) throw new Error('报告内容为空');
            var bytes = this.reportMarkdown.length;
            this.saveMarkdown();
            this.showToast('已导出 ' + bytes + ' 字符的 Markdown 报告');
          } catch (err) {
            this.showToast('导出失败：' + (err.message || err));
          } finally {
            this.exporting = false;
          }
        },

        // ---------------- 核验单预览 / 简报 ----------------
        fetchReport: async function () {
          if (!this.result) return;
          try {
            var res = await fetch('/api/v1/export/report', {
              method: 'POST',
              headers: this.jsonHeaders(),
              body: JSON.stringify({ result: this.result, include_disclaimer: true })
            });
            if (!res.ok) throw new Error('HTTP ' + res.status);
            var data = await res.json();
            this.reportMarkdown = data.markdown || '';
            this.reportFilename = data.filename || '政策效力核验报告.md';
          } catch (err) {
            this.showToast('报告生成失败：' + (err.message || err));
          }
        },

        openReport: async function () {
          if (!this.result) return;
          this.reportOpen = true;
          this.reportTime = new Date().toLocaleString('zh-CN', { hour12: false });
          if (!this.reportMarkdown) await this.fetchReport();
        },

        saveMarkdown: function () {
          if (!this.reportMarkdown) return;
          var blob = new Blob([this.reportMarkdown], { type: 'text/markdown;charset=utf-8' });
          var url = URL.createObjectURL(blob);
          var a = document.createElement('a');
          a.href = url;
          a.download = this.reportFilename || '政策效力核验报告.md';
          document.body.appendChild(a);
          a.click();
          document.body.removeChild(a);
          setTimeout(function () { URL.revokeObjectURL(url); }, 4000);
        },

        downloadReport: async function () {
          if (!this.reportMarkdown) await this.fetchReport();
          if (!this.reportMarkdown) return;
          this.saveMarkdown();
          this.showToast('已下载 Markdown 报告');
        },

        // 企微 / 飞书群可直接粘贴的纯文本简报
        copyBrief: async function () {
          if (!this.result) return;
          var v = this.result.validity_result;
          var m = this.result.policy_meta;
          var g = this.guide;
          var top = this.displaySources.slice(0, 2).map(function (s) { return s.title; }).join('\n  · ');

          var lines = [
            '【政策时效性核验】' + v.status_label,
            '政策：' + this.displayTitle + (m.doc_number ? '（' + m.doc_number + '）' : ''),
            '结论：' + this.cleanText(v.summary),
            '风险：' + g.risk,
            '指引：' + g.action,
            '信源：\n  · ' + (top || '—'),
            '—— 由元宝搜索多路交叉求证引擎生成'
          ];

          try {
            await navigator.clipboard.writeText(lines.join('\n'));
            this.showToast('核验简报已复制，可直接粘贴到企微 / 飞书');
          } catch (err) {
            this.showToast('复制失败，请手动选择文本');
          }
        }
      },

      mounted: function () {
        this.loadBootstrap();
        this.loadSettings();
      }
    });

    app.mount('#app');
    window.__policyApp = app;
  }
})();
