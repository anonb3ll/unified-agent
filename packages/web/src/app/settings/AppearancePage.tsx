/**
 * Appearance (spec §9.7): theme (light, dark, high contrast, system) and
 * density, through the existing theme provider. The theme persists the way
 * the legacy toggle persists it (`localStorage["ua-web-theme"]`); see
 * `appearance.ts` for System and density.
 */
import { useEffect, useState } from "react";
import { RadioGroup } from "../../lib/ui";
import { useTheme } from "../../lib/theme/ThemeProvider";
import { useEpoch } from "../lib/epoch";
import { chooseDensity, chooseTheme, followSystem, readDensity, systemTheme, themeChoice, type Density, type ThemeChoice } from "./appearance";
import { Section, SettingsHeader, useLatest } from "./shared";

const THEME_LABEL: Record<string, string> = { light: "Light", dark: "Dark", "high-contrast": "High contrast" };

export function AppearancePage() {
  const { theme, setTheme } = useTheme();
  const storage = useEpoch().sharedStorage;
  const [choice, setChoice] = useState<ThemeChoice>(() => themeChoice(storage, theme));
  const [density, setDensity] = useState<Density>(() => readDensity(storage));
  const themeRef = useLatest(theme);

  // A theme picked elsewhere (the account menu) replaces "System".
  useEffect(() => setChoice(themeChoice(storage, theme)), [storage, theme]);
  useEffect(() => followSystem(storage, () => themeRef.current, setTheme), [storage, setTheme, themeRef]);

  return (
    <>
      <SettingsHeader title="Appearance" description="How the Control Center looks in this browser. Your choice is kept on this device." />
      <div className="flex flex-col gap-4">
        <Section id="appearance-theme" title="Theme">
          <RadioGroup
            legend="Theme"
            legendHidden
            name="theme"
            value={choice}
            onChange={(value) => {
              const next = value as ThemeChoice;
              chooseTheme(storage, next, setTheme);
              setChoice(next);
            }}
            options={[
              { value: "light", label: "Light", description: "Dark text on a light background." },
              { value: "dark", label: "Dark", description: "Light text on a dark background." },
              { value: "high-contrast", label: "High contrast", description: "The strongest contrast, for low vision or bright light." },
              { value: "system", label: "Same as this device", description: `Follows your device's light, dark or more-contrast setting. Now: ${THEME_LABEL[systemTheme()]}.` },
            ]}
          />
          <p className="m-0 text-sm text-[var(--ua-muted)]" role="status">
            Showing {THEME_LABEL[theme] ?? theme}
            {choice === "system" ? ", from your device's setting" : ""}.
          </p>
        </Section>
        <Section id="appearance-density" title="Density">
          <RadioGroup
            legend="Density"
            legendHidden
            name="density"
            value={density}
            onChange={(value) => {
              const next = value === "compact" ? "compact" : "comfortable";
              chooseDensity(storage, next);
              setDensity(next);
            }}
            options={[
              { value: "comfortable", label: "Comfortable", description: "More space between rows and controls." },
              { value: "compact", label: "Compact", description: "Slightly smaller text and spacing, so more fits on screen." },
            ]}
          />
        </Section>
      </div>
    </>
  );
}
