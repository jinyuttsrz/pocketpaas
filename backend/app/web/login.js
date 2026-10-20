/* 登录 / 注册页交互逻辑
 * 依赖后端接口：
 *   GET  /api/auth/status   探测鉴权开关 / 是否可注册 / 是否已有用户 / 当前是否已登录
 *   POST /api/auth/login    登录（成功即写入 HttpOnly Cookie）
 *   POST /api/auth/register 注册（首个用户自动成为管理员）
 */
(function () {
  "use strict";

  var API = "/api";

  var el = function (id) { return document.getElementById(id); };
  var form = el("authForm");
  var tabLogin = el("tabLogin");
  var tabRegister = el("tabRegister");
  var usernameInput = el("username");
  var passwordInput = el("password");
  var captchaField = el("captchaField");
  var captchaInput = el("captcha");
  var captchaImg = el("captchaImg");
  var captchaPic = el("captchaPic");
  var captchaHint = el("captchaHint");
  var displayNameField = el("displayNameField");
  var displayNameInput = el("displayName");
  var togglePwd = el("togglePwd");
  var pwdStrength = el("pwdStrength");
  var pwdStrengthLabel = el("pwdStrengthLabel");
  var rememberBox = el("remember");
  var forgotBtn = el("forgotBtn");
  var alertBox = el("alertBox");
  var submitBtn = el("submitBtn");
  var submitText = el("submitText");
  var spinner = el("spinner");
  var switchBtn = el("switchBtn");
  var switchTip = el("switchTip");
  var defaultTip = el("defaultTip");
  var formTitle = el("formTitle");
  var formHint = el("formHint");
  var toast = el("toast");

  var mode = "login";
  var allowRegister = true;
  var captchaEnabled = true;
  var captchaUuid = "";
  var captchaLoading = false;
  var submitting = false;

  function showAlert(msg) {
    alertBox.textContent = msg;
    alertBox.classList.remove("hidden");
  }
  function hideAlert() {
    alertBox.classList.add("hidden");
    alertBox.textContent = "";
  }

  var toastTimer = null;
  function showToast(msg, type) {
    toast.textContent = msg;
    toast.className = "toast " + (type || "");
    if (toastTimer) clearTimeout(toastTimer);
    toastTimer = setTimeout(function () { toast.classList.add("hidden"); }, 2600);
  }

  function setLoading(on) {
    submitting = on;
    submitBtn.disabled = on;
    if (on) {
      spinner.classList.remove("hidden");
    } else {
      spinner.classList.add("hidden");
    }
  }

  async function request(path, options) {
    var opts = Object.assign({ credentials: "same-origin" }, options || {});
    var res = await fetch(API + path, opts);
    var data = null;
    var text = await res.text();
    if (text) {
      try { data = JSON.parse(text); } catch (e) { data = null; }
    }
    return { ok: res.ok, status: res.status, data: data };
  }

  function setMode(next) {
    mode = next;
    hideAlert();

    var isLogin = next === "login";
    tabLogin.classList.toggle("active", isLogin);
    tabRegister.classList.toggle("active", !isLogin);

    formTitle.textContent = isLogin ? "欢迎回来" : "创建账号";
    formHint.textContent = isLogin ? "请登录以进入控制台" : "注册后将自动登录并进入控制台";

    displayNameField.classList.toggle("hidden", isLogin);
    pwdStrength.classList.toggle("hidden", isLogin);
    if (isLogin) {
      passwordInput.setAttribute("autocomplete", "current-password");
      submitText.textContent = "登 录";
      switchTip.innerHTML = '还没有账号？<button type="button" class="link" id="switchBtn">立即注册</button>';
    } else {
      passwordInput.setAttribute("autocomplete", "new-password");
      submitText.textContent = "注 册";
      switchTip.innerHTML = '已有账号？<button type="button" class="link" id="switchBtn">返回登录</button>';
    }
    switchBtn = el("switchBtn");
    if (switchBtn) switchBtn.addEventListener("click", function () {
      setMode(mode === "login" ? "register" : "login");
    });

    updateStrength();
  }

  function calcStrength(pwd) {
    if (!pwd) return 0;
    var score = 0;
    if (pwd.length >= 8) score++;
    if (pwd.length >= 12) score++;
    if (/[a-z]/.test(pwd) && /[A-Z]/.test(pwd)) score++;
    if (/\d/.test(pwd)) score++;
    if (/[^A-Za-z0-9]/.test(pwd)) score++;
    if (score <= 1) return 1;
    if (score === 2) return 2;
    if (score === 3) return 3;
    return 4;
  }

  var strengthText = { 1: "弱", 2: "一般", 3: "较强", 4: "很强" };
  function updateStrength() {
    if (mode !== "register") return;
    var pwd = passwordInput.value;
    var level = calcStrength(pwd);
    pwdStrength.className = "password-strength" + (level ? " s" + level : "");
    pwdStrengthLabel.textContent = pwd ? "密码强度：" + strengthText[level] : "密码强度";
  }

  async function refreshCaptcha() {
    if (!captchaEnabled || captchaLoading) return;
    captchaLoading = true;
    captchaInput.value = "";
    captchaHint.hidden = false;
    captchaHint.textContent = "加载中…";
    try {
      var r = await request("/auth/captcha");
      if (r.ok && r.data && r.data.img) {
        captchaUuid = r.data.uuid || "";
        captchaPic.src = r.data.img;
        captchaPic.hidden = false;
        captchaHint.hidden = true;
      } else {
        captchaUuid = "";
        captchaPic.hidden = true;
        captchaHint.textContent = "点击重试";
      }
    } catch (err) {
      captchaUuid = "";
      captchaPic.hidden = true;
      captchaHint.textContent = "点击重试";
    } finally {
      captchaLoading = false;
    }
  }

  async function onSubmit(e) {
    e.preventDefault();
    if (submitting) return;
    hideAlert();

    var username = usernameInput.value.trim();
    var password = passwordInput.value;

    if (!username) { showAlert("请输入用户名"); usernameInput.focus(); return; }
    if (!password) { showAlert("请输入密码"); passwordInput.focus(); return; }

    var captchaCode = captchaInput.value.trim();
    if (captchaEnabled) {
      if (!captchaCode) { showAlert("请输入验证码"); captchaInput.focus(); return; }
      if (!captchaUuid) { showAlert("验证码未加载，请点击图片刷新"); refreshCaptcha(); return; }
    }

    var path = mode === "login" ? "/auth/login" : "/auth/register";
    var body = { username: username, password: password };
    if (captchaEnabled) {
      body.captcha_uuid = captchaUuid;
      body.captcha_code = captchaCode;
    }
    if (mode === "register") {
      var dn = displayNameInput.value.trim();
      if (dn) body.display_name = dn;
    }

    setLoading(true);
    try {
      var r = await request(path, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body)
      });

      if (r.ok) {
        showToast(mode === "login" ? "登录成功，正在进入控制台…" : "注册成功，正在进入控制台…", "ok");
        setTimeout(function () { location.href = "/"; }, 500);
        return;
      }

      var detail = (r.data && r.data.detail) || "操作失败，请稍后重试";
      if (Array.isArray(detail)) {
        detail = detail.map(function (d) { return d.msg; }).join("；");
      }
      showAlert(detail);
      if (captchaEnabled) refreshCaptcha();
    } catch (err) {
      showAlert("网络异常，请检查后端服务是否运行");
    } finally {
      setLoading(false);
    }
  }

  async function init() {
    tabLogin.addEventListener("click", function () { setMode("login"); });
    tabRegister.addEventListener("click", function () { setMode("register"); });
    form.addEventListener("submit", onSubmit);

    togglePwd.addEventListener("click", function () {
      var isPwd = passwordInput.type === "password";
      passwordInput.type = isPwd ? "text" : "password";
      togglePwd.classList.toggle("showing", isPwd);
    });

    passwordInput.addEventListener("input", updateStrength);
    captchaImg.addEventListener("click", refreshCaptcha);

    forgotBtn.addEventListener("click", function () {
      showToast("请联系平台管理员重置密码", "");
    });

    try {
      var r = await request("/auth/status");
      var s = (r.data || {});
      allowRegister = s.allow_register !== false;
      captchaEnabled = s.captcha_enabled !== false;
      document.getElementById("brandFoot").textContent =
        "容器云平台 · v0.1.0" + (s.auth_enabled === false ? " · 未开启鉴权" : "");

      if (!captchaEnabled) {
        captchaField.classList.add("hidden");
      } else {
        await refreshCaptcha();
      }

      if (s.authenticated) {
        location.href = "/";
        return;
      }

      if (!allowRegister) {
        tabRegister.classList.add("hidden");
        switchTip.classList.add("hidden");
      } else {
        if (s.has_user === false) {
          defaultTip.classList.remove("hidden");
          defaultTip.innerHTML = "平台暂无账号，首次注册的账号将自动成为<b>管理员</b>。";
        }
      }
    } catch (err) {
      showToast("无法连接后端服务", "err");
    }

    setMode("login");
    usernameInput.focus();

    if (/[?&]mode=register/.test(location.search) && allowRegister) {
      setMode("register");
    }
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
