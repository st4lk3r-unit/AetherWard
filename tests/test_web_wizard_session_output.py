from cli._html_js import _HTML_JS
from cli._html_ui import _HTML_UI


def test_web_wizard_defaults_to_session_policy_not_fixed_path():
    assert 'wiz-output-policy' in _HTML_UI
    assert 'Use default sessions path' in _HTML_UI
    assert 'path_policy = "default"' in _HTML_JS
    assert 'output_path = "~/.aetherward/sessions/session.jsonl"' not in _HTML_JS
    assert 'value="~/.aetherward/sessions/session.jsonl"' not in _HTML_UI


def test_web_wizard_session_choice_is_on_second_step():
    step1 = _HTML_UI.split('<!-- Step 2: Operating mode + session output -->', 1)[0]
    step2 = _HTML_UI.split('<!-- Step 2: Operating mode + session output -->', 1)[1].split('<!-- Step 3: Antennas -->', 1)[0]
    step6 = _HTML_UI.split('<!-- Step 6: Advanced settings', 1)[1].split('<!-- Trilateration-specific -->', 1)[0]
    assert 'wiz-output-policy' not in step1
    assert 'wiz-output-policy' in step2
    assert 'wiz-output-custom' in step2
    assert 'wiz-output-policy' not in step6
    assert 'Session output is selected on step 2' in step6


def test_web_wizard_asks_config_array_name_as_first_prompt():
    step1 = _HTML_UI.split('<!-- Step 2: Operating mode + session output -->', 1)[0]
    step2 = _HTML_UI.split('<!-- Step 2: Operating mode + session output -->', 1)[1].split('<!-- Step 3: Antennas -->', 1)[0]
    step7 = _HTML_UI.split('<!-- Step 7: Review -->', 1)[1]
    assert 'id="wiz-name"' in step1
    assert 'Config name' in step1
    assert 'First prompt by design' in step1
    assert 'id="wiz-name-err"' in step1
    assert 'id="wiz-name"' not in step2
    assert 'id="wiz-name"' not in step7
    assert _HTML_UI.count('id="wiz-name"') == 1


def test_web_wizard_generated_array_id_uses_config_name_input():
    assert "function wizNameChanged" in _HTML_JS
    assert "document.getElementById('wiz-name')?.value" in _HTML_JS
    assert '`array_id = "${name}"`' in _HTML_JS
    assert "const name=(document.getElementById('wiz-name')?.value||W.configName||'my-config').trim()||'my-config';" in _HTML_JS


def test_web_wizard_has_seven_steps_after_dedicated_name_prompt():
    assert 'const STEPS = 7;' in _HTML_JS
    assert 'Step 1 of 7' in _HTML_UI
    assert '<!-- Step 7: Review -->' in _HTML_UI
    assert 'if(wStep===6) wizShowAdvanced();' in _HTML_JS


def test_web_wizard_requires_name_before_leaving_first_prompt():
    assert "if(wStep===1)" in _HTML_JS
    assert "wiz-name-err" in _HTML_JS
    assert "Enter a config name before continuing." in _HTML_UI
