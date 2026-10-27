// Copyright 2026 The Blink Standalone Authors. BSD-3-Clause; see /LICENSE.
#include "standalone/engine/rendering_smoke.h"

#include <algorithm>
#include <atomic>
#include <chrono>
#include <cstdlib>
#include <iostream>
#include <span>
#include <string>
#include <string_view>
#include <thread>
#include <vector>

#include "standalone/engine/rendering_fixtures.h"

#if defined(__clang__)
#define RENDERING_UNSAFE_BUFFERS(...)        \
  _Pragma("clang unsafe_buffer_usage begin") \
      __VA_ARGS__ _Pragma("clang unsafe_buffer_usage end")
#else
#define RENDERING_UNSAFE_BUFFERS(...) __VA_ARGS__
#endif

namespace blink_standalone::smoke {
namespace {
constexpr unsigned kSize = 80;
std::atomic<unsigned> image_fetches{0};
std::atomic<unsigned> font_fetches{0};
std::atomic<unsigned> missing_font_fetches{0};

bool Check(be_status status, const char* action) {
  if (status == BE_OK)
    return true;
  std::cerr << action << ": " << be_status_string(status) << '\n';
  return false;
}

bool Assert(bool condition, const char* action) {
  if (!condition)
    std::cerr << "Assertion failed: " << action << '\n';
  return condition;
}

bool Script(be_page* page, std::string_view source, std::string& output) {
  be_script_result result{};
  result.struct_size = sizeof(result);
  result.abi_version = BE_ABI_VERSION;
  if (!Check(be_page_evaluate_script(page, source.data(), source.size(),
                                     "https://engine.invalid/rendering.js",
                                     &result),
             "rendering regression JavaScript") ||
      !Assert(!result.is_exception && (result.utf8_data || !result.utf8_size),
              "rendering script owns a valid result")) {
    return false;
  }
  RENDERING_UNSAFE_BUFFERS(output.assign(
      result.utf8_data ? result.utf8_data : "", result.utf8_size);)
  return true;
}

bool Evaluate(be_page* page, std::string_view source, const char* action) {
  std::string result;
  return Script(page, source, result) && Assert(result == "true", action);
}

bool Wait(be_engine* engine,
          be_page* page,
          std::string_view expression,
          const char* action) {
  const auto deadline =
      std::chrono::steady_clock::now() + std::chrono::seconds(4);
  do {
    if (!Check(be_engine_pump(engine, 8), "rendering regression pump"))
      return false;
    std::string result;
    if (!Script(page, expression, result))
      return false;
    if (result == "true")
      return true;
    std::this_thread::sleep_for(std::chrono::milliseconds(2));
  } while (std::chrono::steady_clock::now() < deadline);
  return Assert(false, action);
}

bool Load(be_engine* engine, be_page* page, std::string_view html) {
  return Check(be_page_resize(page, kSize, kSize), "rendering fixture size") &&
         Check(be_page_load_html(page, html.data(), html.size(),
                                 "https://engine.invalid/rendering"),
               "rendering fixture HTML") &&
         Wait(engine, page, "document.readyState==='complete'",
              "rendering fixture document completed");
}

bool Paint(be_page* page, std::vector<uint8_t>& pixels, unsigned size = kSize) {
  pixels.assign(size_t(size) * size * 4, 0);
  return Check(be_page_paint_rgba(page, pixels.data(), pixels.size(), size * 4),
               "rendering regression native RGBA paint");
}

bool HasInk(const std::vector<uint8_t>& pixels) {
  unsigned ink = 0;
  for (size_t offset = 0; offset + 3 < pixels.size(); offset += 4) {
    if (pixels[offset] < 128 && pixels[offset + 1] < 128 &&
        pixels[offset + 2] < 128 && pixels[offset + 3] == 255) {
      ++ink;
    }
  }
  return ink > 30;
}

bool Color(const std::vector<uint8_t>& pixels,
           unsigned size,
           unsigned x,
           unsigned y,
           int red,
           int green,
           int blue,
           int tolerance = 0) {
  const auto offset = (size_t(y) * size + x) * 4;
  return std::abs(int(pixels[offset]) - red) <= tolerance &&
         std::abs(int(pixels[offset + 1]) - green) <= tolerance &&
         std::abs(int(pixels[offset + 2]) - blue) <= tolerance &&
         pixels[offset + 3] == 255;
}

template <size_t Size>
void Response(const uint8_t (&bytes)[Size],
              const char* mime,
              be_resource_response& response) {
  response.data = bytes;
  response.size = Size;
  response.mime_type = mime;
  response.charset = nullptr;
  response.release = nullptr;
  response.release_user_data = nullptr;
}

std::string ImageHtml() {
  return std::string(
             "<!doctype html><meta charset=utf-8><style>"
             "html,body{margin:0;background:white}img{position:absolute;"
             "width:20px;height:20px;image-rendering:pixelated}</style>"
             "<img id=png style='left:0;top:0' src='/rendering/red.png'>"
             "<img id=jpeg style='left:20px;top:0' src='/rendering/green.jpg'>"
             "<img id=webp style='left:40px;top:0' src='/rendering/blue.webp'>"
             "<img id=data style='left:0;top:20px' src='") +
         fixtures::kRedPngDataUrl + "'>";
}

bool ImagePixels(be_page* page, unsigned size = kSize) {
  std::vector<uint8_t> pixels;
  return Paint(page, pixels, size) &&
         Assert(Color(pixels, size, 8, 8, 255, 0, 0) &&
                    Color(pixels, size, 28, 8, 0, 255, 0, 3) &&
                    Color(pixels, size, 48, 8, 0, 0, 255) &&
                    Color(pixels, size, 8, 28, 255, 0, 0) &&
                    Color(pixels, size, 72, 72, 255, 255, 255),
                "PNG, JPEG, WebP and data URL decode to actual native pixels");
}

bool ImagesReady(be_engine* engine, be_page* page) {
  return Wait(engine, page,
              "Array.from(document.images).length===4 && "
              "Array.from(document.images).every(i=>i.complete && "
              "i.naturalWidth===2 && i.naturalHeight===2)",
              "all four image decoder fixtures complete");
}

bool FontPixels(be_page* page,
                std::string_view settings,
                std::vector<uint8_t>& pixels) {
  const std::string source =
      "(()=>{target.style.fontVariantAlternates='normal';"
      "target.style.fontFeatureSettings='normal';" +
      std::string(settings) + ";return true})()";
  return Evaluate(page, source, "font feature control installation") &&
         Paint(page, pixels) && Assert(HasInk(pixels), "font control has ink");
}
}  // namespace

bool FetchRenderingFixture(const char* url, be_resource_response* response) {
  if (!url || !response)
    return false;
  const std::string_view resource(url);
  if (resource == "https://engine.invalid/rendering/red.png") {
    ++image_fetches;
    Response(fixtures::kRedPng, "image/png", *response);
  } else if (resource == "https://engine.invalid/rendering/green.jpg") {
    ++image_fetches;
    Response(fixtures::kGreenJpeg, "image/jpeg", *response);
  } else if (resource == "https://engine.invalid/rendering/blue.webp") {
    ++image_fetches;
    Response(fixtures::kBlueWebp, "image/webp", *response);
  } else if (resource == "https://engine.invalid/rendering/features.ttf") {
    ++font_fetches;
    Response(fixtures::kFeatureFont, "font/ttf", *response);
  } else {
    if (resource == "https://engine.invalid/rendering/unavailable.ttf")
      ++missing_font_fetches;
    return false;
  }
  return true;
}

bool ImageRenderingSmoke(be_engine* engine, be_page* page) {
  const unsigned before = image_fetches.load();
  const auto html = ImageHtml();
  if (!Load(engine, page, html) || !ImagesReady(engine, page) ||
      !ImagePixels(page) ||
      !Assert(image_fetches.load() >= before + 3,
              "external image fixtures use host resource callbacks") ||
      !Check(be_page_resize(page, 96, 96), "image decoder resize") ||
      !ImagePixels(page, 96) ||
      !Check(be_page_resize(page, kSize, kSize), "restore smoke page size")) {
    return false;
  }
  // The process-global decoder cache must remain valid when one page goes away.
  be_page_config config{};
  config.struct_size = sizeof(config);
  config.abi_version = BE_ABI_VERSION;
  config.width = kSize;
  config.height = kSize;
  be_page* peer = nullptr;
  if (!Check(be_page_create(engine, &config, &peer), "image peer creation"))
    return false;
  const bool peer_ok = Load(engine, peer, html) && ImagesReady(engine, peer) &&
                       ImagePixels(peer);
  const bool destroyed = Check(be_page_destroy(peer), "image peer destruction");
  return peer_ok && destroyed && ImagePixels(page);
}

bool FontRenderingSmoke(be_engine* engine, be_page* page) {
  std::vector<uint8_t> pixels;
  // Unknown aliases do not remove CSS or prevent ordinary text rendering.
  for (const char* family :
       {"sans-serif", "system-ui", "'Noto Sans'", "WaveAbsentFamily"}) {
    const std::string html =
        "<!doctype html><meta charset=utf-8><style>html,body{margin:0;"
        "background:white}#target{font-size:24px;line-height:32px;color:black;"
        "font-family:" +
        std::string(family) +
        ";font-variant-alternates:styleset(disambiguation)}</style>"
        "<div id=target>ABC abc</div>";
    if (!Load(engine, page, html) ||
        !Evaluate(
            page,
            "getComputedStyle(target).fontVariantAlternates === "
            "'styleset(disambiguation)' && target.textContent==='ABC abc'",
            "font variant CSS remains unchanged") ||
        !Paint(page, pixels) ||
        !Assert(HasInk(pixels),
                "generic, named and missing families paint ink"))
      return false;
  }
  const unsigned unavailable_before = missing_font_fetches.load();
  static constexpr char unavailable[] =
      "<!doctype html><style>@font-face{font-family:WaveUnavailable;"
      "src:url('/rendering/unavailable.ttf');font-display:swap}"
      "html,body{margin:0;background:white}#target{font:24px/32px "
      "WaveUnavailable,sans-serif;font-variant-alternates:styleset(missing)}"
      "</style><div id=target>Fallback ABC</div>";
  if (!Load(engine, page, unavailable) ||
      !Check(be_page_layout(page), "start unavailable font fallback") ||
      !Wait(engine, page, "document.fonts.status==='loaded'",
            "unavailable webfont settles to fallback") ||
      !Paint(page, pixels) ||
      !Assert(HasInk(pixels), "webfont failure paints") ||
      !Assert(missing_font_fetches.load() > unavailable_before,
              "webfont fallback exercised a denied real resource request")) {
    return false;
  }

  const unsigned before = font_fetches.load();
  static constexpr char feature_html[] =
      "<!doctype html><style>@font-face{font-family:'Wave Smoke Features';"
      "src:url('/rendering/features.ttf')}"
      "@font-feature-values 'Wave Smoke Features'{@styleset{alternate:2;}}"
      "html,body{margin:0;background:white}#target{position:absolute;left:0;"
      "top:0;width:80px;height:80px;color:black;white-space:pre;"
      "font:64px/80px 'Wave Smoke Features',sans-serif}"
      "</style><div id=target>A</div>"
      "<script>window.fixtureFontReady=false;document.fonts.load("
      "'64px \"Wave Smoke Features\"','A').then(f=>{"
      "window.fixtureFontReady=f.length===1})</script>";
  if (!Load(engine, page, feature_html) ||
      !Wait(engine, page, "window.fixtureFontReady===true",
            "authored feature font loads through actual Blink font decoder") ||
      !Assert(font_fetches.load() > before, "feature font uses host fetch")) {
    return false;
  }
  std::vector<uint8_t> plain, alias, direct, historical, direct_historical;
  std::vector<uint8_t> overridden;
  if (!FontPixels(page, "", plain) ||
      !FontPixels(page,
                  "target.style.fontVariantAlternates='styleset(alternate)'",
                  alias) ||
      !FontPixels(page, "target.style.fontFeatureSettings='\"ss02\" 1'",
                  direct) ||
      !Assert(alias == direct && alias != plain,
              "family alias ss02 exactly matches direct feature pixels and "
              "changes the glyph") ||
      !FontPixels(page, "target.style.fontVariantAlternates='historical-forms'",
                  historical) ||
      !FontPixels(page, "target.style.fontFeatureSettings='\"hist\" 1'",
                  direct_historical) ||
      !Assert(historical == direct_historical && historical != plain,
              "historical forms exactly match hist feature pixels") ||
      !FontPixels(page,
                  "target.style.fontVariantAlternates='styleset(alternate)';"
                  "target.style.fontFeatureSettings='\"ss02\" 0'",
                  overridden) ||
      !Assert(overridden == plain && overridden != alias,
              "explicit font-feature-settings overrides alias feature")) {
    return false;
  }
  // The authored font deliberately lacks CJK: force real character fallback
  // with an alternate-bearing description rather than only family fallback.
  if (!Evaluate(page,
                "target.style.fontVariantAlternates='styleset(alternate)';"
                "target.style.fontFeatureSettings='normal';"
                "target.textContent='A\\u4e2d';true",
                "missing glyph fallback fixture installation") ||
      !Paint(page, pixels) ||
      !Assert(HasInk(pixels), "missing glyph fallback remains renderable") ||
      !Evaluate(page,
                "target.textContent==='A\\u4e2d' && "
                "getComputedStyle(target).fontVariantAlternates === "
                "'styleset(alternate)'",
                "character fallback retains CSS and source text")) {
    return false;
  }
  return Check(be_page_resize(page, kSize, kSize), "final smoke page size");
}
}  // namespace blink_standalone::smoke
