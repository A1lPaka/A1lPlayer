from PySide6.QtCore import Qt
from PySide6.QtGui import QCloseEvent
from PySide6.QtWidgets import QWidget

from models.ThemeColor import ThemeState
from ui.PlayerControls import PlayerControls
from ui.SubtitleGenerationDialog import SubtitleGenerationDialog
from ui.SubtitleProgressDialog import SubtitleProgressDialog
from utils import Metrics


def _metrics() -> Metrics:
    return Metrics(
        min_window_side=900,
        scale_factor=1.0,
        window_width=720,
        window_height=450,
        icon_size=24,
        font_size=16,
        menu_width=90,
        theme_dialog_width=450,
        theme_dialog_height=514,
        pip_min_width=173,
        subtitle_dialog_width=450,
        subtitle_dialog_height=270,
        subtitle_progress_dialog_width=378,
        subtitle_progress_dialog_height=180,
    )


def test_subtitle_generation_dialog_initial_values_and_generate_state(workspace_tmp_path):
    media_path = str(workspace_tmp_path / "movie.mkv")
    dialog = SubtitleGenerationDialog(ThemeState(), _metrics(), media_path=media_path)

    assert dialog.output_path_input.text().endswith("movie.srt")
    assert dialog.output_format_combo.currentData() == "srt"
    assert dialog.model_combo.currentData() == "small"
    assert dialog.audio_track_combo.currentText() == dialog.AUDIO_TRACKS_LOADING_LABEL
    assert dialog.audio_track_combo.isEnabled() is False
    assert dialog.generate_button.isEnabled() is False

    dialog.set_generate_enabled(True)

    assert dialog.generate_button.isEnabled() is True


def test_subtitle_generation_dialog_output_format_and_audio_track_selection(workspace_tmp_path):
    media_path = str(workspace_tmp_path / "movie.mkv")
    dialog = SubtitleGenerationDialog(ThemeState(), _metrics(), media_path=media_path)

    dialog.set_audio_tracks([(None, "Current / default"), (2, "Audio 2 | ENG")])
    dialog.set_audio_track_selector_enabled(True)
    dialog.set_generate_enabled(True)
    dialog.set_selected_audio_track(2)
    dialog.output_format_combo.setCurrentIndex(dialog.output_format_combo.findData("vtt"))
    dialog.audio_language_combo.setCurrentIndex(dialog.audio_language_combo.findData("en"))
    dialog.device_combo.setCurrentIndex(dialog.device_combo.findData("cpu"))

    result = dialog.get_result()

    assert dialog.output_path_input.text().endswith("movie.vtt")
    assert result.audio_stream_index == 2
    assert result.audio_language == "en"
    assert result.device == "cpu"
    assert result.output_format == "vtt"
    assert result.auto_open_after_generation is True


def test_subtitle_generation_dialog_emits_generate_and_cancel_once(workspace_tmp_path):
    dialog = SubtitleGenerationDialog(ThemeState(), _metrics(), media_path=str(workspace_tmp_path / "movie.mkv"))
    generated = []
    canceled = []
    dialog.generateRequested.connect(generated.append)
    dialog.canceled.connect(lambda: canceled.append(True))
    dialog.set_generate_enabled(True)

    dialog.generate_button.click()
    dialog.close_button.click()
    dialog.closeEvent(QCloseEvent())

    assert len(generated) == 1
    assert len(canceled) == 1


def test_subtitle_progress_dialog_updates_state_and_cancel_once():
    dialog = SubtitleProgressDialog(ThemeState(), _metrics())
    cancel_calls = []
    dialog.cancelRequested.connect(lambda: cancel_calls.append(True))

    dialog.set_status("Transcribing audio")
    dialog.set_progress(150)
    dialog.set_details("Source: C:/very/long/path/movie.mkv\nStage: Transcribing")
    dialog.set_cancel_enabled(False, "Stopping...")
    dialog.cancel_button.click()
    dialog.set_cancel_enabled(True)
    dialog.cancel_button.click()

    assert dialog.progress_bar.value() == 100
    assert dialog.status_label.text() == "Transcribing audio (100%)"
    assert "Stage: Transcribing" in dialog.details_label.text()
    assert dialog.cancel_button.text() == "Cancel"
    assert cancel_calls == [True]


def test_subtitle_progress_dialog_indeterminate_and_service_close():
    dialog = SubtitleProgressDialog(ThemeState(), _metrics())

    dialog.set_status("Installing GPU runtime")
    dialog.set_indeterminate(True)

    assert dialog.progress_bar.minimum() == 0
    assert dialog.progress_bar.maximum() == 0
    assert dialog.status_label.text() == "Installing GPU runtime"

    dialog.set_indeterminate(False)
    dialog.set_progress(25)

    assert dialog.progress_bar.maximum() == 100
    assert dialog.status_label.text() == "Installing GPU runtime (25%)"


def test_player_controls_basic_buttons_and_state():
    parent = QWidget()
    controls = PlayerControls(parent, _metrics(), ThemeState())

    controls.toggle_play_pause(True)
    controls.toggle_fullscreen(True)
    controls.toggle_muted(True)
    controls.set_speed_value(1.25)
    controls.update_timing(30_000, 120_000)
    controls.volume_controls.volume_bar.set_volume(0.42)
    controls.toggle_progress_seekable(True)

    assert controls.play_pause_button.is_playing is True
    assert controls.fullscreen_button.is_fullscreen is True
    assert controls.volume_controls.volume_button.is_muted is True
    assert controls.speed_label.text() == "x1.25"
    assert controls.current_time.text() == "00:30"
    assert controls.total_time.text() == "02:00"
    assert controls.progress_bar.value == 0.25
    assert controls.current_volume_percent() == 42
    assert controls.progress_bar.testAttribute(Qt.WA_TransparentForMouseEvents) is False


def test_player_controls_pip_mode_and_theme_application():
    theme = ThemeState(
        {
            "control_button_color": (20, 30, 40),
            "volume_bar_color_active": (50, 60, 70),
            "progress_bar_color_active": (80, 90, 100),
        }
    )
    parent = QWidget()
    controls = PlayerControls(parent, _metrics(), ThemeState())

    controls.set_pip_mode(True)
    controls.apply_theme(theme)

    assert controls.fullscreen_button.isVisible() is False
    assert controls.pip_button.isVisible() is False
    assert controls.speed_label.isVisible() is False
    assert controls.play_pause_button.bg_color == (20, 30, 40)
    assert controls.volume_controls.volume_bar.active_bg_color == (50, 60, 70)
    assert controls.progress_bar.active_bg_color == (80, 90, 100)
