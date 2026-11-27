import { AdditiveBlending, DoubleSide, MeshStandardMaterial, ShaderMaterial, type IUniform } from "three";

/** Shared by all materials; written once per frame. */
export const uniforms = {
  uTime: { value: 0 } as IUniform<number>,
  /** 0 = reduced motion: nothing pulses or travels. */
  uMotion: { value: 1 } as IUniform<number>,
  /** 0 = live, 1 = the values on screen are not current: the scene loses its colour. */
  uStale: { value: 0 } as IUniform<number>,
};

const COMMON = /* glsl */ `
uniform float uTime, uMotion, uStale;
float hash11(float p) { p = fract(p * .1031); p *= p + 33.33; p *= p + p; return fract(p); }
float hash21(vec2 p) { vec3 q = fract(vec3(p.xyx) * .1031); q += dot(q, q.yzx + 33.33); return fract((q.x + q.y) * q.z); }
float dither() { return fract(52.9829189 * fract(dot(gl_FragCoord.xy, vec2(.06711056, .00583715)))); }
vec3 staleOf(vec3 c) { return mix(c, vec3(dot(c, vec3(.3, .59, .11))) * .55, uStale); }
// How bright a status light is now. y: 0 at the foot, 1 at the top. All below three flashes a second.
float pulse(float code, float y, float seed) {
  if (code < .5) return .5;
  if (uMotion < .5) return code > 4.5 ? .18 : 1.;
  if (code < 1.5) return .62 + .38 * sin(uTime * 2.4 + seed * 6.);
  if (code < 2.5) return .55 + .6 * max(0., sin(uTime * 8.5));
  if (code < 3.5) return .1 + 1.15 * step(.45, fract(uTime * 1.2));
  if (code < 4.5) { float d = fract(uTime * .55) * 1.3 - .15 - y; return .22 + 1.2 * exp(-d * d * 40.); }
  return .18;
}`;

/** Fragment helpers only exist in fragment shaders (gl_FragCoord). */
const VERT_COMMON = /* glsl */ `uniform float uTime, uMotion;`;

/** The material of all components: physically lit metal, with lights that show the twin's state. */
export function componentMaterial(height: number): MeshStandardMaterial {
  const m = new MeshStandardMaterial({ vertexColors: true, metalness: 0.62, roughness: 0.42, envMapIntensity: 1 });
  m.onBeforeCompile = (s) => {
    Object.assign(s.uniforms, uniforms, { uHeight: { value: height } });
    s.vertexShader = s.vertexShader
      .replace("#include <common>", /* glsl */ `#include <common>
        attribute float aPart;
        attribute vec3 iColor;
        attribute vec4 iFx; // status code, load, visible, highlight
        attribute vec2 iFx2; // seconds since the status changed, seed
        uniform float uHeight;
        varying float vPart; varying vec3 vStatus; varying vec4 vFx; varying vec2 vFx2; varying vec2 vPUv; varying float vY;`)
      .replace("#include <begin_vertex>", /* glsl */ `#include <begin_vertex>
        vPart = aPart; vStatus = iColor; vFx = iFx; vFx2 = iFx2; vPUv = uv; vY = position.y / uHeight;`);
    s.fragmentShader = s.fragmentShader
      .replace("#include <common>", /* glsl */ `#include <common>
        ${COMMON}
        varying float vPart; varying vec3 vStatus; varying vec4 vFx; varying vec2 vFx2; varying vec2 vPUv; varying float vY;`)
      .replace("#include <clipping_planes_fragment>", /* glsl */ `#include <clipping_planes_fragment>
        if (vFx.z < .995 && vFx.z < dither()) discard;`)
      .replace("#include <emissivemap_fragment>", /* glsl */ `#include <emissivemap_fragment>
        {
          float code = floor(vFx.x + .5), load = vFx.y, seed = vFx2.y;
          bool known = load >= 0.;
          bool dead = code > 2.5 && code < 3.5;
          bool ghost = code > 4.5;
          float beat = pulse(code, vY, seed);
          vec3 glow = vec3(0.);
          if (vPart < .5) { // body
            if (code > 1.5 && code < 2.5) glow += vec3(1., .22, .03) * .3 * beat * smoothstep(.1, 1., vY);
            if (dead) glow += vec3(.9, .03, .08) * .1 * beat;
          } else if (vPart < 1.5) { // status light
            glow += vStatus * beat * 2.6;
          } else if (vPart < 2.5) { // faces of server units
            vec2 cell = vPUv * vec2(18., 22.);
            vec2 id = floor(cell), f = fract(cell);
            float unit = step(.12, fract(vPUv.y * 11.)) ;
            diffuseColor.rgb = mix(vec3(.004), vec3(.035, .042, .052), unit);
            float vent = step(.35, f.x) * step(.3, f.y) * step(id.x, 11.);
            diffuseColor.rgb *= mix(1., .35, vent * unit);
            bool led = id.x > 12.5 && id.x < 17.5 && unit > .5;
            vec2 q = f - .5;
            float dot_ = smoothstep(.3, .14, length(q));
            if (led) {
              float r = hash21(id + floor(seed * 97.));
              float act = known ? .12 + .88 * load : 0.;
              float blink = step(hash21(id * 1.7 + floor(uTime * uMotion * (2. + 7. * r))), act);
              vec3 lamp = r > .82 ? vec3(.9, .95, 1.) : r > .3 ? vec3(.1, .55, 1.) : vStatus;
              if (dead) { lamp = vStatus; blink = step(.9, r) * beat; }
              if (ghost || !known && !dead) { lamp = vec3(.25, .3, .38); blink = step(.75, r) * .4; }
              glow += lamp * dot_ * (.08 + 1.15 * blink);
            }
          } else if (vPart < 3.5) { // load gauge
            float seg = step(.22, fract(vPUv.y * 18.));
            float on = known && !dead && !ghost ? step(vPUv.y, max(load, .03)) : 0.;
            glow += vStatus * seg * (.03 + 1.5 * on);
          } else if (vPart < 4.5) { // trim
            glow += vec3(.25, .5, .8) * (dead || ghost ? .03 : .55);
          } else if (vPart < 5.5) { // screen: only the status, no invented curves
            float line = .75 + .25 * step(.5, fract(vPUv.y * 24.));
            vec2 e = smoothstep(vec2(0.), vec2(.06), vPUv) * smoothstep(vec2(0.), vec2(.06), 1. - vPUv);
            glow += vStatus * line * e.x * e.y * (dead ? .5 * beat : ghost ? .06 : .4);
          } else if (vPart < 6.5) { // boundary to the outside
            vec2 c = (vPUv - .5) * vec2(1., 1.45);
            float r = length(c), a = atan(c.y, c.x);
            float w = .5 + .5 * sin(a * 3. + r * 26. - uTime * uMotion * 1.6);
            float open = dead || ghost ? .12 : .6;
            if (dither() > (.18 + .35 * w) * smoothstep(.75, .1, r) + .05) discard;
            glow += vStatus * (.15 + w * .6) * open;
          } else { // vents
            float slot = step(.5, fract(vPUv.y * 26.)) * step(.08, vPUv.x) * step(vPUv.x, .92);
            diffuseColor.rgb *= mix(1., .25, slot);
          }
          // A change of status sweeps over the object once.
          float age = vFx2.x;
          if (age < 1.2) { float d = age * 1.3 - vY; glow += vStatus * exp(-d * d * 60.) * 1.4 * uMotion; }
          // Hovered or selected: an outline of light.
          float rim = pow(1. - clamp(dot(normal, normalize(vViewPosition)), 0., 1.), 3.);
          glow += vec3(.3, .62, 1.) * vFx.w * (rim * .55 + .018);
          totalEmissiveRadiance += staleOf(glow);
          if (ghost) diffuseColor.rgb = mix(diffuseColor.rgb, vec3(.09, .1, .12), .6);
          diffuseColor.rgb = staleOf(diffuseColor.rgb);
        }`);
  };
  m.customProgramCacheKey = () => "simucore-component";
  return m;
}

/** Status ring under each component: filled arc = load, waves = overload or change. */
export function ringMaterial(): ShaderMaterial {
  return new ShaderMaterial({
    uniforms, transparent: true, depthWrite: false,
    vertexShader: /* glsl */ `
      ${VERT_COMMON}
      attribute vec4 iPlace; // x, z, half width, half depth
      attribute vec3 iColor; attribute vec4 iFx; attribute vec2 iFx2; attribute vec2 iAi; // anomaly, risk
      varying vec2 vP; varying vec2 vHalf; varying vec3 vColor; varying vec4 vFx; varying vec2 vFx2; varying vec2 vAi;
      void main() {
        vec2 reach = iPlace.zw + 1.1;
        vP = position.xy * 2. * reach; vHalf = iPlace.zw; vColor = iColor; vFx = iFx; vFx2 = iFx2; vAi = iAi;
        gl_Position = projectionMatrix * viewMatrix * vec4(iPlace.x + vP.x, .015, iPlace.y - vP.y, 1.);
      }`,
    fragmentShader: /* glsl */ `
      ${COMMON}
      varying vec2 vP; varying vec2 vHalf; varying vec3 vColor; varying vec4 vFx; varying vec2 vFx2; varying vec2 vAi;
      float box(vec2 p, vec2 b, float r) { vec2 q = abs(p) - b + r; return length(max(q, 0.)) + min(max(q.x, q.y), 0.) - r; }
      void main() {
        float code = floor(vFx.x + .5);
        float d = box(vP, vHalf + .16, .3);
        float px = fwidth(d);
        float line = 1. - smoothstep(.03, .03 + px, abs(d - .02));
        float beat = pulse(code, .5, vFx2.y);
        float a = line * (.35 + .65 * beat);
        if (code > 4.5) a *= .7 * step(.5, fract((vP.x + vP.y) * 1.6)); // unknown: dashed
        // light on the floor
        a += exp(-max(d, 0.) * 3.2) * .22 * beat * step(0., d + .4);
        // overloaded and failed: waves running outwards
        if (code > 1.5 && code < 3.5 && uMotion > .5) {
          float w = fract(uTime * (code < 2.5 ? .9 : .6));
          a += (1. - smoothstep(0., .05, abs(d - w * .95))) * (1. - w) * .9;
        }
        // a change of status: one larger wave
        if (vFx2.x < 1.1 && uMotion > .5) a += (1. - smoothstep(0., .09, abs(d - vFx2.x * .9))) * (1.1 - vFx2.x);
        // selected: corner brackets
        vec2 q = abs(vP) - (vHalf + .42);
        float corner = (1. - smoothstep(.035, .035 + px, abs(max(q.x, q.y)))) * step(-.42, min(q.x, q.y));
        vec3 c = mix(vColor, vec3(.55, .82, 1.), corner * vFx.w);
        a = max(a, corner * vFx.w);
        // the AI's opinions lie outside the status ring and have their own colours
        float turn = atan(vP.y / vHalf.y, vP.x / vHalf.x) / 6.28318 + .5;
        float an = (1. - smoothstep(.04, .04 + px, abs(d - .5))) * step(.02, vAi.x)
                 * step(.45, fract(turn * 14. + uTime * uMotion * .22)); // anomaly: a ring of moving dashes
        c = mix(c, vec3(.44, .26, 1.) * (.9 + vAi.x), an); a = max(a, an * .95);
        float ri = (1. - smoothstep(.055, .055 + px, abs(d - .72))) * step(.02, vAi.y); // risk: an arc as long as it is probable
        float full = step(fract(turn + .25), vAi.y);
        float beatR = uMotion > .5 ? .75 + .25 * sin(uTime * 4.) : 1.;
        c = mix(c, vec3(1., .22, .55) * (full > .5 ? 1.25 * beatR : .25), ri); a = max(a, ri * (full > .5 ? 1. : .45));
        a *= vFx.z * (1. - smoothstep(.8, 1.05, d));
        if (a < .004) discard;
        gl_FragColor = vec4(staleOf(c) * (1. + .5 * line), a);
        #include <tonemapping_fragment>
        #include <colorspace_fragment>
      }`,
  });
}

/** Icon plates above the components, always turned to the viewer, and warning signs above those. */
export function plateMaterial(atlas: IUniform): ShaderMaterial {
  return new ShaderMaterial({
    uniforms: { ...uniforms, uAtlas: atlas, uCells: { value: 8 } },
    transparent: true, depthWrite: false, side: DoubleSide,
    vertexShader: /* glsl */ `
      ${VERT_COMMON}
      attribute vec4 iPos; // x, y, z, size
      attribute vec3 iColor;
      attribute vec4 iFx; // status code, visible, highlight, seed
      attribute vec2 iWhat; // icon, 0 = plate / 1 = sign / 2, 3 = opinion of the AI
      varying vec2 vUvP; varying vec3 vColor; varying vec4 vFx; varying vec2 vWhat;
      void main() {
        vUvP = uv; vColor = iColor; vFx = iFx; vWhat = iWhat;
        vec3 at = iPos.xyz;
        if (iWhat.y > .5 && iWhat.y < 1.5) at.y += .1 * sin(uTime * 3.2 + iFx.w * 6.) * uMotion;
        vec4 mv = viewMatrix * vec4(at, 1.);
        float size = iPos.w * (1. + .12 * iFx.z) * step(.03, iFx.y);
        // never smaller than a readable symbol, never a wall close up
        size = clamp(size, -mv.z * .014, -mv.z * .075);
        mv.xy += position.xy * size * step(.03, iFx.y);
        if (iWhat.y > 1.5) mv.x += (iWhat.y > 2.5 ? -1. : 1.) * clamp(.95, -mv.z * .02, -mv.z * .1); // beside the plate
        mv.z += .25;
        gl_Position = projectionMatrix * mv;
      }`,
    fragmentShader: /* glsl */ `
      ${COMMON}
      uniform sampler2D uAtlas; uniform float uCells;
      varying vec2 vUvP; varying vec3 vColor; varying vec4 vFx; varying vec2 vWhat;
      float hexa(vec2 p) { p = abs(p); return max(p.x * .866 + p.y * .5, p.y) ; }
      void main() {
        vec2 p = vUvP * 2. - 1.;
        float code = floor(vFx.x + .5);
        float beat = pulse(code, .5, vFx.w);
        vec2 cell = vec2(mod(vWhat.x, uCells), uCells - 1. - floor(vWhat.x / uCells));
        float icon = texture2D(uAtlas, (cell + clamp((vUvP - .5) * 1.75 + .5, .01, .99)) / uCells).a;
        if (max(abs(p.x), abs(p.y)) * 1.75 > 1.) icon = 0.;
        vec3 c; float a;
        if (vWhat.y < .5) {
          float d = hexa(p.yx);
          float px = fwidth(d);
          float inside = 1. - smoothstep(.86 - px, .86, d);
          float edge = smoothstep(.76 - px, .76, d) * inside;
          c = mix(vec3(.012, .02, .034), vColor * (.6 + .75 * beat), edge);
          c = mix(c, code > 4.5 ? vec3(.5) : vec3(.9, .95, 1.), icon);
          c += vec3(.2, .45, .7) * vFx.z * .22 * (inside - edge);
          a = inside * .93;
        } else {
          float d = length(p);
          float px = fwidth(d);
          float inside = 1. - smoothstep(.9 - px, .9, d);
          c = mix(vColor * (.5 + .5 * beat), vec3(.02, .02, .03), icon);
          a = inside;
        }
        if (a * vFx.y < .01) discard;
        gl_FragColor = vec4(staleOf(c), a * vFx.y);
        #include <tonemapping_fragment>
        #include <colorspace_fragment>
      }`,
  });
}

const LINK_FETCH = /* glsl */ `
  uniform highp sampler2D uLinks;
  vec4 link(int row, int col) { return texelFetch(uLinks, ivec2(col, row), 0); }
  vec3 bezier(vec3 a, vec3 b, vec3 c, vec3 d, float t) {
    float u = 1. - t; return u*u*u*a + 3.*u*u*t*b + 3.*u*t*t*c + t*t*t*d; }
  vec3 slope(vec3 a, vec3 b, vec3 c, vec3 d, float t) {
    float u = 1. - t; return 3.*u*u*(b-a) + 6.*u*t*(c-b) + 3.*t*t*(d-c); }`;

/** Columns of the link texture. */
export const LINK_COLS = 6;

/** All connections: one tube template, bent in the vertex shader along each link's curve. */
export function wireMaterial(links: IUniform): ShaderMaterial {
  return new ShaderMaterial({
    uniforms: { ...uniforms, uLinks: links }, transparent: true, depthWrite: false,
    vertexShader: /* glsl */ `
      ${VERT_COMMON}
      ${LINK_FETCH}
      varying float vT; varying float vLen; varying vec4 vA; varying vec3 vColor; varying float vVisible; varying float vFlow; varying float vKind; varying vec3 vN; varying vec3 vV;
      void main() {
        int row = gl_InstanceID;
        vec4 a = link(row, 0), b = link(row, 1), c = link(row, 2), d = link(row, 3);
        vA = link(row, 4); vColor = link(row, 5).rgb; vVisible = d.w; vFlow = b.w; vKind = a.w; vT = position.x;
        vec3 p = bezier(a.xyz, b.xyz, c.xyz, d.xyz, position.x);
        vec3 t = normalize(slope(a.xyz, b.xyz, c.xyz, d.xyz, clamp(position.x, .001, .999)) + vec3(1e-5));
        vec3 side = normalize(cross(t, abs(t.y) > .95 ? vec3(1., 0., 0.) : vec3(0., 1., 0.)));
        vec3 up = cross(side, t);
        vLen = distance(a.xyz, d.xyz) * 1.15;
        float r = (a.w < 2.5 ? .02 + .022 * b.w : .03) + .01 * vA.z;
        float dist = -(viewMatrix * vec4(p, 1.)).z;
        r = max(r, dist * .00085) * step(.02, d.w);
        vec3 n = side * cos(position.y) + up * sin(position.y);
        vec4 mv = viewMatrix * vec4(p + n * r, 1.);
        vN = normalize(mat3(viewMatrix) * n); vV = normalize(-mv.xyz);
        gl_Position = projectionMatrix * mv;
      }`,
    fragmentShader: /* glsl */ `
      ${COMMON}
      varying float vT; varying float vLen; varying vec4 vA; varying vec3 vColor; varying float vVisible; varying float vFlow; varying float vKind; varying vec3 vN; varying vec3 vV;
      void main() {
        float code = floor(vA.y + .5);
        if (vVisible < .01) discard;
        float dash = fract(vT * vLen * 1.5);
        if ((code > 2.5 && code < 3.5 && dash > .62) || (code > 4.5 && dash > .4)) discard;
        bool cable = vKind > 2.5;
        vec3 calm = cable ? (vKind < 3.5 ? vec3(.3, .36, .48) : vKind < 4.5 ? vec3(.07, .42, .5) : vec3(.36, .25, .62))
                          : mix(vec3(.05, .11, .19), vec3(.07, .3, .62), clamp(vFlow * 1.6, 0., 1.));
        vec3 c = code < .5 ? calm : vColor * pulse(code, vT, vA.w) * .8;
        float lit = .55 + .45 * max(dot(vN, normalize(vec3(.3, 1., .4))), 0.);
        c = c * lit + (code < .5 ? vec3(.14, .38, .8) : vColor) * vA.z * .5;
        gl_FragColor = vec4(staleOf(c), min(1., vVisible));
        #include <tonemapping_fragment>
        #include <colorspace_fragment>
      }`,
  });
}

export const PER_LINK = 18;

/** Requests: their number and speed follow the measured traffic (logarithmic), red ones its failures. */
export function particleMaterial(links: IUniform, count: number): ShaderMaterial {
  return new ShaderMaterial({
    // with hundreds of links the lights become smaller, or they would hide the components
    uniforms: { ...uniforms, uLinks: links, uDensity: { value: Math.min(1, Math.sqrt(80 / Math.max(1, count))) } },
    transparent: true, depthWrite: false, blending: AdditiveBlending,
    vertexShader: /* glsl */ `
      ${VERT_COMMON}
      ${LINK_FETCH}
      uniform float uDensity;
      varying vec2 vP; varying vec3 vColor; varying float vAlpha;
      float hash(float p) { p = fract(p * .1031); p *= p + 33.33; p *= p + p; return fract(p); }
      void main() {
        int row = gl_InstanceID / ${PER_LINK}, k = gl_InstanceID - row * ${PER_LINK};
        vec4 a = link(row, 0), b = link(row, 1), c = link(row, 2), d = link(row, 3), s = link(row, 4);
        float flow = b.w, code = floor(s.y + .5);
        float count = flow > .001 ? max(1., ceil(flow * ${PER_LINK}.)) : 0.;
        bool on = float(k) < count && d.w > .05 && code < 4.5;
        float t = fract(s.x + float(k) / max(count, 1.));
        vec3 p = bezier(a.xyz, b.xyz, c.xyz, d.xyz, t);
        vec4 mv = viewMatrix * vec4(p, 1.);
        bool lost = code > 2.5 && code < 3.5;
        bool bad = lost || hash(float(k) + floor(s.w * 91.)) < c.w;
        vColor = bad ? vec3(1., .16, .22) : vec3(.35, .8, 1.);
        // a failed request does not arrive
        vAlpha = smoothstep(0., .05, t) * smoothstep(1., .95, t) * d.w;
        if (bad) vAlpha *= 1. - smoothstep(.55, .78, t);
        float size = max((.11 + .06 * s.z) * mix(.5, 1., uDensity), -mv.z * .0046 * uDensity);
        vAlpha *= mix(.45, 1., uDensity);
        if (!on) size = 0.;
        vP = position.xy * 2.;
        mv.xy += position.xy * 2. * size;
        gl_Position = projectionMatrix * mv;
      }`,
    fragmentShader: /* glsl */ `
      ${COMMON}
      varying vec2 vP; varying vec3 vColor; varying float vAlpha;
      void main() {
        float d = length(vP);
        if (d > 1.) discard;
        float core = smoothstep(.45, 0., d), halo = pow(1. - d, 2.2);
        gl_FragColor = vec4(staleOf(mix(vColor, vec3(1.), core * .7)) * (halo + core * 1.6), vAlpha * halo);
        #include <tonemapping_fragment>
        #include <colorspace_fragment>
      }`,
  });
}

/** Markings on the floor that show which racks belong to which machine. */
export function zoneMaterial(): ShaderMaterial {
  return new ShaderMaterial({
    uniforms, transparent: true, depthWrite: false,
    vertexShader: /* glsl */ `
      attribute vec4 iRect; attribute float iShow;
      varying vec2 vP; varying vec2 vHalf; varying float vShow;
      void main() {
        vHalf = (iRect.zw - iRect.xy) * .5; vShow = iShow;
        vP = position.xy * 2. * vHalf;
        vec2 c = (iRect.xy + iRect.zw) * .5;
        gl_Position = projectionMatrix * viewMatrix * vec4(c.x + vP.x, .006, c.y - vP.y, 1.);
      }`,
    fragmentShader: /* glsl */ `
      ${COMMON}
      varying vec2 vP; varying vec2 vHalf; varying float vShow;
      void main() {
        vec2 q = abs(vP) - vHalf + .35;
        float d = length(max(q, 0.)) + min(max(q.x, q.y), 0.) - .35;
        float px = fwidth(d);
        float line = 1. - smoothstep(.015, .015 + px, abs(d + .06));
        float a = line * .3 + .045 * step(d, -.06);
        gl_FragColor = vec4(staleOf(vec3(.32, .5, .72)), a * vShow);
        #include <tonemapping_fragment>
        #include <colorspace_fragment>
      }`,
  });
}

/** Raised floor of 60 cm tiles, some perforated. */
export function floorMaterial(): MeshStandardMaterial {
  const m = new MeshStandardMaterial({ color: "#1a2029", metalness: 0.25, roughness: 0.38, envMapIntensity: 0.7 });
  m.onBeforeCompile = (s) => {
    s.vertexShader = s.vertexShader
      .replace("#include <common>", "#include <common>\nvarying vec3 vW;")
      .replace("#include <begin_vertex>", "#include <begin_vertex>\nvW = (modelMatrix * vec4(position, 1.)).xyz;");
    s.fragmentShader = s.fragmentShader
      .replace("#include <common>", /* glsl */ `#include <common>
        varying vec3 vW;
        float h21(vec2 p) { vec3 q = fract(vec3(p.xyx) * .1031); q += dot(q, q.yzx + 33.33); return fract((q.x + q.y) * q.z); }`)
      .replace("#include <color_fragment>", /* glsl */ `#include <color_fragment>
        {
          vec2 g = vW.xz / 1.2;
          vec2 id = floor(g), f = fract(g);
          vec2 fw = fwidth(g);
          vec2 e = min(f, 1. - f);
          float far = clamp(1. - max(fw.x, fw.y) * 3., 0., 1.);
          float seam = 1. - smoothstep(0., 1., min(e.x / max(fw.x * 1.2, .012), e.y / max(fw.y * 1.2, .012)));
          float tile = h21(id);
          diffuseColor.rgb *= .92 + .16 * tile;
          if (tile > .86) { // perforated
            vec2 c = fract(f * 11.) - .5;
            float hole = smoothstep(.34, .22, length(c)) * step(.07, e.x) * step(.07, e.y);
            diffuseColor.rgb *= 1. - .75 * hole * clamp(1. - max(fw.x, fw.y) * 14., 0., 1.);
          }
          diffuseColor.rgb *= 1. - .55 * seam * far;
        }`);
  };
  m.customProgramCacheKey = () => "simucore-floor";
  return m;
}
