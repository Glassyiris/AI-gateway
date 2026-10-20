local home = os.getenv("APISIX_HOME") or "/usr/local/apisix"
package.path = home .. "/?.lua;" .. home .. "/deps/share/lua/5.1/?.lua;" ..
    home .. "/deps/share/lua/5.1/?/init.lua;" .. package.path
package.cpath = home .. "/deps/lib/lua/5.1/?.so;" ..
    home .. "/deps/lib64/lua/5.1/?.so;" .. package.cpath

local json = require("cjson.safe")
local source = (arg[0]:match("^(.*)/") or ".") .. "/routing_selector.lua"
local real_ngx = ngx
local total = 0

-- Shared dict that persists across run() calls in one test, like the real
-- ngx.shared zone across requests in one worker.
local function new_shared_dict()
    local store = {}
    return {
        get = function(_, key) return store[key] end,
        set = function(_, key, value, _) store[key] = value return true end,
        delete = function(_, key) store[key] = nil return true end,
        _store = store,
    }
end

local function run(options)
    options = options or {}
    local calls = {}
    local headers = {Authorization = options.authorization or "Bearer caller", session_id = "session"}
    local body = options.body or '{"model":"axis/model + exact","stream":true}'
    local rewritten
    local response_body = options.lookup_body or ('{"decision":"' .. (options.decision or "cpa") .. '","generation":7}')
    local log_lines = {}
    local fake = {
        req = {
            read_body = function() end,
            get_body_data = function() return body end,
            get_body_file = function() return nil end,
            set_body_data = function(value) rewritten = value end,
            set_header = function(name, value) headers[name] = value end,
        },
        escape_uri = function(value)
            return (value:gsub("%%", "%%25"):gsub("/", "%%2F"):gsub(" ", "%%20"):gsub("%+", "%%2B"))
        end,
        log = function(_, ...) log_lines[#log_lines + 1] = table.concat({...}) end,
        WARN = 5,
        shared = options.shared and {routing_lkg = options.shared} or nil,
    }
    package.loaded["resty.http"] = {
        new = function()
            return {
                set_timeouts = function() end,
                connect = function()
                    if options.connect_error then return nil, "connect failed" end
                    return true
                end,
                request = function(_, request)
                    calls[#calls + 1] = request
                    if options.request_error then return nil, "request failed" end
                    local sent = false
                    return {
                        status = options.lookup_status or 200,
                        headers = { ["Content-Length"] = tostring(#response_body) },
                        body_reader = function()
                            if sent then return nil end
                            sent = true
                            return response_body
                        end,
                    }
                end,
                set_keepalive = function() return true end,
                close = function() end,
            }
        end,
    }
    _G.ngx = fake
    local module = dofile(source)
    local ctx = {}
    local ok, status, result, response_headers = pcall(module.run, nil, ctx, "cpa-secret")
    _G.ngx = real_ngx
    assert(ok, status)
    return status, result, response_headers, ctx, calls, headers, rewritten, log_lines
end

local function test(name, fn)
    fn()
    total = total + 1
    print("PASS " .. name)
end

test("CPA decision selects one upstream and injects only the CPA credential", function()
    local status, _, _, ctx, calls, headers = run({decision = "cpa"})
    assert(status == nil and ctx.upstream_id == "cpa")
    assert(#calls == 1 and calls[1].method == "GET")
    assert(calls[1].path == "/routing-index")
    assert(calls[1].query == "model=axis%2Fmodel%20%2B%20exact")
    assert(headers.Authorization == "Bearer cpa-secret")
    assert(headers.session_id == "session")
end)

test("AISIX decision preserves the caller credential and exact body", function()
    local status, _, _, ctx, calls, headers, rewritten = run({decision = "aisix"})
    assert(status == nil and ctx.upstream_id == "aisix" and #calls == 1)
    assert(headers.Authorization == "Bearer caller")
    assert(rewritten == nil)
end)

test("not found and unavailable fail without an upstream", function()
    local status, body, response_headers, ctx, calls = run({decision = "not_found"})
    assert(status == 404 and body:find('"code":"model_not_found"', 1, true))
    assert(not body:find("cpa-secret", 1, true) and not body:find("Bearer caller", 1, true) and not body:find("axis/model", 1, true))
    assert(response_headers["Content-Type"] == "application/json" and ctx.upstream_id == nil and #calls == 1)
    local unavailable_status, unavailable_body, _, unavailable_ctx, unavailable_calls = run({decision = "unavailable"})
    assert(unavailable_status == 503 and unavailable_body:find('"code":"service_unavailable"', 1, true))
    assert(unavailable_ctx.upstream_id == nil and #unavailable_calls == 1)
end)

test("lookup failures never become not found or a backend selection", function()
    for _, options in ipairs({
        {connect_error = true},
        {request_error = true},
        {lookup_status = 500},
        {lookup_body = '{"decision":"unknown","generation":7}'},
        {lookup_body = '{"decision":"cpa"}'},
        {lookup_body = string.rep("x", 1025)},
    }) do
        local status, body, _, ctx = run(options)
        assert(status == 503 and body:find('"code":"service_unavailable"', 1, true))
        assert(ctx.upstream_id == nil)
    end
end)

test("malformed classified payloads do not perform a lookup", function()
    for _, body in ipairs({"not-json", "[]", "{}", '{"model":""}', '{"model":42}'}) do
        local status, result, _, ctx, calls = run({body = body})
        assert(status == 400 and result:find('"code":"invalid_request_error"', 1, true))
        assert(not result:find("cpa-secret", 1, true) and not result:find("Bearer caller", 1, true))
        assert(ctx.upstream_id == nil and #calls == 0)
    end
end)

test("existing prompt cache compatibility rewrite remains scoped", function()
    local _, _, _, ctx, _, _, rewritten = run({
        decision = "cpa",
        body = '{"model":"cpa/model","prompt_cache_retention":"24h","value":1}',
    })
    assert(ctx.upstream_id == "cpa" and rewritten)
    assert(not rewritten:find("prompt_cache_retention", 1, true))
    local document = json.decode(rewritten)
    assert(document and document.model == "cpa/model", rewritten)
end)

test("authoritative decision stores last-known-good in shared dict", function()
    local shared = new_shared_dict()
    local status, _, _, ctx = run({decision = "aisix", shared = shared})
    assert(status == nil and ctx.upstream_id == "aisix")
    assert(shared._store["axis/model + exact"] == "aisix")
end)

test("transport failure uses fresh stored decision and warns once", function()
    local shared = new_shared_dict()
    run({decision = "cpa", shared = shared})
    for _, options in ipairs({{connect_error = true}, {request_error = true}, {lookup_status = 500}, {lookup_body = "not-json"}}) do
        options.shared = shared
        local status, _, _, ctx, calls, _, _, log_lines = run(options)
        assert(status == nil and ctx.upstream_id == "cpa")
        assert(#calls <= 1) -- one bounded lookup attempt; connect errors never reach request()
        assert(#log_lines == 1 and log_lines[1]:find("last%-known%-good"))
        assert(log_lines[1]:find("axis/model") and not log_lines[1]:find("cpa%-secret"))
    end
end)

test("transport failure without a stored decision stays 503", function()
    local shared = new_shared_dict()
    local status, body, _, ctx = run({connect_error = true, shared = shared})
    assert(status == 503 and body:find('"code":"service_unavailable"', 1, true) and ctx.upstream_id == nil)
    -- same, but entirely without a shared dict configured
    status, body, _, ctx = run({connect_error = true})
    assert(status == 503 and ctx.upstream_id == nil)
end)

test("not_found and unavailable delete the stored decision", function()
    local shared = new_shared_dict()
    run({decision = "cpa", shared = shared})
    assert(shared._store["axis/model + exact"] == "cpa")
    local status, _, _, ctx = run({decision = "not_found", shared = shared})
    assert(status == 404 and ctx.upstream_id == nil)
    assert(shared._store["axis/model + exact"] == nil)
    -- stored entry must never be resurrected: transport failure now 503s
    local fail_status, _, _, fail_ctx = run({connect_error = true, shared = shared})
    assert(fail_status == 503 and fail_ctx.upstream_id == nil)
    -- unavailable also deletes
    run({decision = "aisix", shared = shared})
    local un_status, _, _, un_ctx = run({decision = "unavailable", shared = shared})
    assert(un_status == 503 and un_ctx.upstream_id == nil and shared._store["axis/model + exact"] == nil)
    local fail2_status = run({connect_error = true, shared = shared})
    assert(fail2_status == 503)
end)

test("stored decision is keyed by exact model ID", function()
    local shared = new_shared_dict()
    run({decision = "cpa", shared = shared, body = '{"model":"model-a"}'})
    local status, _, _, ctx = run({connect_error = true, shared = shared, body = '{"model":"model-b"}'})
    assert(status == 503 and ctx.upstream_id == nil)
end)

print("PASS all " .. total .. " routing selector checks")
