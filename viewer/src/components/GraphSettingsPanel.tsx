import { RotateCcw, X } from "lucide-react";
import type { GraphSettings } from "../types";
import { Button } from "./ui/button";
import { Separator } from "./ui/separator";
import { Slider } from "./ui/slider";

interface Props {
  open: boolean;
  settings: GraphSettings;
  onChange: (next: GraphSettings) => void;
  onClose: () => void;
  onReplay: () => void;
  onReset: () => void;
}

interface ControlProps {
  label: string;
  value: number;
  min: number;
  max: number;
  step: number;
  display: string;
  onChange: (value: number) => void;
}

function RangeControl({label, value, min, max, step, display, onChange}: ControlProps) {
  return <label className="graph-setting-control">
    <span>{label}</span>
    <div><output>{display}</output><Slider aria-label={label} value={[value]} min={min} max={max} step={step} onValueChange={([next])=>onChange(next)}/></div>
  </label>;
}

export default function GraphSettingsPanel({open, settings, onChange, onClose, onReplay, onReset}: Props) {
  if (!open) return null;
  const set = <K extends keyof GraphSettings>(key: K, value: GraphSettings[K]) => onChange({...settings, [key]: value});
  return <aside className="graph-settings-panel" aria-label="그래프 설정">
    <header><strong>그래프 설정</strong><div><Button size="icon-sm" variant="ghost" aria-label="그래프 설정 초기화" onClick={onReset}><RotateCcw/></Button><Button size="icon-sm" variant="ghost" aria-label="그래프 설정 닫기" onClick={onClose}><X/></Button></div></header>
    <section><h2>표시</h2>
      <RangeControl label="노드 크기" value={settings.nodeScale} min={0.3} max={2} step={0.05} display={settings.nodeScale.toFixed(2)} onChange={(value)=>set("nodeScale",value)}/>
      <RangeControl label="링크 두께" value={settings.linkThickness} min={0.15} max={2.5} step={0.05} display={settings.linkThickness.toFixed(2)} onChange={(value)=>set("linkThickness",value)}/>
      <Button className="settings-replay" variant="secondary" size="sm" onClick={onReplay}>애니메이션 재생</Button>
    </section>
    <Separator/>
    <section><h2>장력</h2>
      <RangeControl label="중심 장력" value={settings.centerForce} min={0.05} max={1} step={0.01} display={settings.centerForce.toFixed(2)} onChange={(value)=>set("centerForce",value)}/>
      <RangeControl label="반발력" value={settings.repelForce} min={1} max={20} step={0.5} display={settings.repelForce.toFixed(1)} onChange={(value)=>set("repelForce",value)}/>
      <RangeControl label="링크 장력" value={settings.linkStrength} min={0.1} max={2.5} step={0.05} display={settings.linkStrength.toFixed(2)} onChange={(value)=>set("linkStrength",value)}/>
      <RangeControl label="링크 거리" value={settings.linkDistance} min={80} max={420} step={10} display={String(Math.round(settings.linkDistance))} onChange={(value)=>set("linkDistance",value)}/>
    </section>
  </aside>;
}
