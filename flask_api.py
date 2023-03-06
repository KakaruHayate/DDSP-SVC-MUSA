import io
import logging
import torch
import numpy as np
import slicer
import soundfile as sf
import librosa
from flask import Flask, request, send_file
from flask_cors import CORS

from ddsp.vocoder import load_model, F0_Extractor, Volume_Extractor, Units_Encoder
from enhancer import Enhancer


app = Flask(__name__)

CORS(app)

logging.getLogger("numba").setLevel(logging.WARNING)


@app.route("/voiceChangeModel", methods=["POST"])
def voice_change_model():
    request_form = request.form
    wave_file = request.files.get("sample", None)
    # 变调信息
    f_pitch_change = float(request_form.get("fPitchChange", 0))
    # 获取spk_id
    int_speak_id = int(request_form.get("sSpeakId", 0))
    if enable_spk_id_cover:
        int_speak_id = spk_id
    # print("说话人:" + str(int_speak_id))
    # DAW所需的采样率
    daw_sample = int(float(request_form.get("sampleRate", 0)))
    # http获得wav文件并转换
    input_wav_read = io.BytesIO(wave_file.read())
    # 模型推理
    _audio, _model_sr = svc_model.infer(input_wav_read, f_pitch_change, int_speak_id)
    tar_audio = librosa.resample(_audio, _model_sr, daw_sample)
    # 返回音频
    out_wav_path = io.BytesIO()
    sf.write(out_wav_path, tar_audio, daw_sample, format="wav")
    out_wav_path.seek(0)
    return send_file(out_wav_path, download_name="temp.wav", as_attachment=True)


class SvcDDSP:
    def __init__(self, model_path, vocoder_based_enhancer, input_pitch_extractor,
                 f0_min, f0_max):
        self.model_path = model_path
        self.vocoder_based_enhancer = vocoder_based_enhancer
        self.input_pitch_extractor = input_pitch_extractor
        self.f0_min = f0_min
        self.f0_max = f0_max
        self.device = 'cuda' if torch.cuda.is_available() else 'cpu'
        
        # load ddsp model
        self.model, self.args = load_model(self.model_path, device=self.device)
        
        # load units encoder
        self.units_encoder = Units_Encoder(
            self.args.data.encoder,
            self.args.data.encoder_ckpt,
            self.args.data.encoder_sample_rate,
            self.args.data.encoder_hop_size,
            device=self.device)
        
        # load enhancer
        if self.vocoder_based_enhancer:
            self.enhancer = Enhancer(self.args.enhancer.type, self.args.enhancer.ckpt, device=self.device)

    def infer(self, input_wav, pitch_adjust, speaker_id):
        print("Infer!")
        # load input
        audio, sample_rate = librosa.load(input_wav, sr=None, mono=True)
        if len(audio.shape) > 1:
            audio = librosa.to_mono(audio)
        hop_size = self.args.data.block_size * sample_rate / self.args.data.sampling_rate
        
        # extract f0
        pitch_extractor = F0_Extractor(
            self.input_pitch_extractor,
            sample_rate,
            hop_size,
            float(self.f0_min),
            float(self.f0_max))
        f0 = pitch_extractor.extract(audio, uv_interp=True, device=self.device)
        f0 = torch.from_numpy(f0).float().to(self.device).unsqueeze(-1).unsqueeze(0)
        f0 = f0 * 2 ** (float(pitch_adjust) / 12)
        
        # extract volume
        volume_extractor = Volume_Extractor(hop_size)
        volume = volume_extractor.extract(audio)
        volume = torch.from_numpy(volume).float().to(self.device).unsqueeze(-1).unsqueeze(0)

        # extract units
        audio_t = torch.from_numpy(audio).float().unsqueeze(0).to(device)
        units = self.units_encoder.encode(audio_t, sample_rate, hop_size)
        
        # forward and return the output
        with torch.no_grad():
            output, _, (s_h, s_n) = self.model(units, f0, volume)
            if self.vocoder_based_enhancer:
                output, output_sample_rate = self.enhancer.enhance(output, self.args.data.sampling_rate, f0, self.args.data.block_size)
            else:
                output_sample_rate = self.args.data.sampling_rate

            output = output.squeeze().cpu().numpy()
            return output, output_sample_rate


if __name__ == "__main__":
    # ddsp-svc下只需传入下列参数。
    # 对接的是串串香火锅大佬https://github.com/zhaohui8969/VST_NetProcess-。建议使用2.0版本。
    # flask部分来自diffsvc小狼大佬编写的代码。
    # config和模型得同一目录。
    checkpoint_path = "exp/combsub-test/model_550000.pt"
    # 是否使用预训练的基于声码器的增强器增强输出，正常音域范围内的高音频质量，但对硬件要求更高。
    use_vocoder_based_enhancer = True
    # f0提取器，有parselmouth, dio, harvest, crepe
    select_pitch_extractor = 'crepe'
    # f0范围限制(Hz)
    limit_f0_min = 50
    limit_f0_max = 1100
    # 默认说话人。以及是否优先使用默认说话人覆盖vst传入的参数。
    # 但是ddsp-svc现在还没有多说话人，所以此参数无效。
    spk_id = 37
    enable_spk_id_cover = True

    svc_model = SvcDDSP(checkpoint_path, use_vocoder_based_enhancer, select_pitch_extractor,
                        limit_f0_min, limit_f0_max)

    # 此处与vst插件对应，端口必须接上。
    app.run(port=6844, host="0.0.0.0", debug=False, threaded=False)
