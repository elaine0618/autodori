import logging
from bestdori.songs import get_all
from bestdori.charts import Chart as BestdoriChart


class BestdoriAPI:
    _logger = logging.getLogger("BestdoriAPI")
    
    @staticmethod
    def get_song_list():
        """获取歌曲列表"""
        try:
            songs = get_all(index=5)
            BestdoriAPI._logger.info(f"成功获取 {len(songs)} 首歌曲")
            return songs
        except Exception as e:
            BestdoriAPI._logger.error(f"获取歌曲列表失败: {e}")
            return {}
    
    @staticmethod
    def get_chart(song_id: str, difficulty: str):
        """获取谱面"""
        try:
            chart = BestdoriChart.get_chart(int(song_id), difficulty)
            BestdoriAPI._logger.info(f"成功获取谱面: {song_id}-{difficulty}")
            return chart.to_list()
        except Exception as e:
            BestdoriAPI._logger.error(f"获取谱面失败 {song_id}-{difficulty}: {e}")
            return []