extends Node2D


func _on_player_health_changed(new_health: int) -> void:
	print("Player health: ", new_health)
