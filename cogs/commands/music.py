import discord 
from discord.ext import commands
import re
import lavalink
from datetime import timedelta
from discord.ui import Button, Select, View
from lavalink.events import (
    TrackStartEvent,
    TrackEndEvent,
    QueueEndEvent,
    TrackExceptionEvent,
    TrackStuckEvent
)
from lavalink.errors import ClientError
from lavalink.filters import LowPass
from lavalink.server import LoadType
import sqlite3
import aiosqlite
import asyncio

def progress_bar(current: int, total: int, size: int = 20) -> str:

    completed_marker = '<:bar_full_pink:1449451280232743077>'
    remaining_marker = '<a:bar_empty:1449451253879930982>'
    current_position_marker = '<:cypher:1449451286217887765>'

    def format_duration(duration: int) -> str:
        return str(timedelta(milliseconds=duration))[2:7]  # Extract mm:ss from timedelta

    if current >= total:
        return f"`{format_duration(current)}` {completed_marker * (size - 1)}{current_position_marker} `{format_duration(total)}`"

    progress = int((size - 1) * (current / total))
    remaining = size - 1 - progress

    bar = f"`{format_duration(current)}` {completed_marker * progress}{current_position_marker}{remaining_marker * remaining} `{format_duration(total)}`"
    return bar

class LavalinkVoiceClient(discord.VoiceProtocol):
    def __init__(self, client: discord.Client, channel: discord.abc.Connectable):
        self.client = client
        self.channel = channel
        self.guild_id = channel.guild.id
        self._destroyed = False
        self.lavalink = self.client.lavalink

    async def on_voice_server_update(self, data):
        lavalink_data = {
            't': 'VOICE_SERVER_UPDATE',
            'd': data
        }
        await self.lavalink.voice_update_handler(lavalink_data)

    async def on_voice_state_update(self, data):
        channel_id = data['channel_id']

        if not channel_id:
            await self._destroy()
            return

        self.channel = self.client.get_channel(int(channel_id))

        lavalink_data = {
            't': 'VOICE_STATE_UPDATE',
            'd': data
        }

        await self.lavalink.voice_update_handler(lavalink_data)

    async def connect(self, *, timeout: float, reconnect: bool, self_deaf: bool = False, self_mute: bool = False) -> None:
        """
        Connect the bot to the voice channel and create a player_manager
        if it doesn't exist yet.
        """
        self.lavalink.player_manager.create(guild_id=self.channel.guild.id)
        await self.channel.guild.change_voice_state(channel=self.channel, self_mute=self_mute, self_deaf=self_deaf)

    async def disconnect(self, *, force: bool = False) -> None:
        """
        Handles the disconnect.
        Cleans up running player and leaves the voice client.
        """
        player = self.lavalink.player_manager.get(self.channel.guild.id)

        if not force and not player.is_connected:
            return

        await self.channel.guild.change_voice_state(channel=None)

        player.channel_id = None
        await self._destroy()

    async def _destroy(self):
        self.cleanup()

        if self._destroyed:
            return

        self._destroyed = True

        try:
            await self.lavalink.player_manager.destroy(self.guild_id)
        except ClientError:
            pass
   
class QueueButtonView(View):
    def __init__(self, player, color, author_id):
        super().__init__(timeout=60)
        self.player = player
        self.author_id = author_id
        self.color = color
        self.current_page = 0
        self.total_pages = self.calculate_total_pages()
        self.update_components()

    def calculate_total_pages(self):
        queue = self.player.queue
        pages = (len(queue) + 9) // 10  # 10 tracks per page
        return max(1, pages)

    def build_embed(self, page: int) -> discord.Embed:
        queue = self.player.queue
        current_track = self.player.current
        total_songs = len(queue) + 1  # current track plus queued songs
        total_duration_seconds = (current_track.duration // 1000 if current_track else 0) + sum(track.duration // 1000 for track in queue)
        total_duration_str = str(timedelta(seconds=total_duration_seconds))
        current_duration_str = str(timedelta(seconds=current_track.duration // 1000)) if current_track else "0:00"

        embed = discord.Embed(
            description=(
                f"**[{current_track.title}]({current_track.uri})**\n\n"
                f"{current_track.author}\n\n"
                f"**Current Position:** `1`\n"
                f"*You can use* Add As Upcoming button *to jump to a specific position!*\n\n"
                f"<a:whiteheart:1449451260309934244> `1` [{current_track.title}]({current_track.uri}) ・ {current_track.author} ・ `{current_duration_str}`"
            ),
            color=self.color
        ) if current_track else discord.Embed(description="No track currently playing", color=self.color)

        if current_track:
            embed.set_author(name="Current Track")
            embed.set_thumbnail(url=current_track.artwork_url)

        # Calculate start and end indexes for the page (10 tracks per page)
        start = page * 10
        end = start + 10
        page_tracks = queue[start:end]
        if page_tracks:
            lines = []
            for j, track in enumerate(page_tracks):
                overall_index = start + j + 2  # current track is 1; queued tracks start at 2
                track_duration = str(timedelta(seconds=track.duration // 1000))
                line = f"`{overall_index}` [{track.title}]({track.uri}) ・ {track.author} ・ `{track_duration}`"
                lines.append(line)
            # Build the queue text line-by-line and ensure it doesn't exceed 1024 characters.
            allowed_lines = []
            total_len = 0
            for line in lines:
                # +1 for the newline character
                if total_len + len(line) + 1 > 1024:
                    break
                allowed_lines.append(line)
                total_len += len(line) + 1
            queue_text = "\n\n".join(allowed_lines)
            if len(allowed_lines) < len(lines):
                queue_text += "\n..."
            embed.add_field(name=" ", value=queue_text, inline=False)

        embed.set_footer(text=f"Page {page+1}/{self.total_pages} ・ {total_songs} songs, {total_duration_str}")
        return embed

    def update_components(self):
        self.clear_items()
        # Create a dropdown for removing tracks on the current page
        queue = self.player.queue
        start = self.current_page * 10
        end = start + 10
        page_tracks = queue[start:end]
        options = []
        for j, track in enumerate(page_tracks):
            overall_index = start + j + 2
            options.append(discord.SelectOption(label=f"{overall_index}. {track.title}", value=str(overall_index)))
        if options:
            dropdown = Select(placeholder="Remove a track", options=options)
            dropdown.callback = self.remove_track
            self.add_item(dropdown)

        # Navigation buttons if multiple pages exist
        if self.total_pages > 1:
            if self.current_page > 0:
                prev_button = Button(label="Previous", style=discord.ButtonStyle.secondary)
                prev_button.callback = self.prev_page
                self.add_item(prev_button)
            if self.current_page < self.total_pages - 1:
                next_button = Button(label="Next", style=discord.ButtonStyle.secondary)
                next_button.callback = self.next_page
                self.add_item(next_button)

        # Clear queue button
        clear_button = Button(label="Clear Queue", style=discord.ButtonStyle.danger)
        clear_button.callback = self.clear_queue
        self.add_item(clear_button)

    async def next_page(self, interaction: discord.Interaction):
        if interaction.user.id != self.author_id:
            await interaction.response.send_message("Only the command invoker can use these buttons.", ephemeral=True)
            return
        
        self.current_page += 1
        await interaction.response.edit_message(embed=self.build_embed(self.current_page), view=self)

    async def prev_page(self, interaction: discord.Interaction):
        if interaction.user.id != self.author_id:
            await interaction.response.send_message("Only the command invoker can use these buttons.", ephemeral=True)
            return
        
        self.current_page -= 1
        await interaction.response.edit_message(embed=self.build_embed(self.current_page), view=self)

    async def remove_track(self, interaction: discord.Interaction):
        if interaction.user.id != self.author_id:
            await interaction.response.send_message("Only the command invoker can use these buttons.", ephemeral=True)
            return
        
        # The dropdown returns the overall index (as a string)
        value = interaction.data["values"][0]
        try:
            overall_index = int(value)
        except ValueError:
            await interaction.response.send_message("Invalid selection.", ephemeral=True)
            return

        # Convert overall index to an index in the queue (current track is not part of the queue)
        track_index = overall_index - 2
        queue = self.player.queue
        if 0 <= track_index < len(queue):
            removed_track = queue.pop(track_index)
            await interaction.response.send_message(f"Removed `{removed_track.title}` from the queue.", ephemeral=True)
            self.total_pages = self.calculate_total_pages()
            if self.current_page >= self.total_pages:
                self.current_page = self.total_pages - 1
            self.update_components()
            await interaction.message.edit(embed=self.build_embed(self.current_page), view=self)
        else:
            await interaction.response.send_message("Track not found.", ephemeral=True)

    async def clear_queue(self, interaction: discord.Interaction):
        if interaction.user.id != self.author_id:
            await interaction.response.send_message("Only the command invoker can use these buttons.", ephemeral=True)
            return
        
        self.player.queue.clear()
        await interaction.response.send_message("Queue cleared!", ephemeral=True)
        self.total_pages = self.calculate_total_pages()
        self.current_page = 0
        self.update_components()
        await interaction.message.edit(embed=self.build_embed(self.current_page), view=self)

    async def start(self, ctx):
        self.total_pages = self.calculate_total_pages()
        message = await ctx.reply(embed=self.build_embed(self.current_page), view=self)
        self.message = message
        
class MusicControlView(discord.ui.View):
    def __init__(self, player):
        super().__init__(timeout=None)
        self.player = player  
        self.color = discord.Color.red()
        
    @discord.ui.button(label="Loop", style=discord.ButtonStyle.danger)
    async def add_to_loop(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not interaction.user.voice:
            await interaction.response.send_message("You are not in a voice channel.", ephemeral=True)
            return
        if interaction.user.voice.channel != interaction.guild.voice_client.channel:
            await interaction.response.send_message("<:stolen_emoji:1449451348759416912> You are not in the same voice channel.", ephemeral=True)
            return
        
        if self.player.loop == lavalink.DefaultPlayer.LOOP_SINGLE:
            self.player.loop = lavalink.DefaultPlayer.LOOP_NONE
            embed = discord.Embed(description=" Loop has been **disabled**.", color=discord.Color.red())
        else:
            self.player.loop = lavalink.DefaultPlayer.LOOP_SINGLE
            embed = discord.Embed(description=" Loop has been **enabled for current track**.", color=discord.Color.green())

        await interaction.response.send_message(embed=embed, ephemeral=True)
            
    @discord.ui.button(label="Pause", style=discord.ButtonStyle.secondary)
    async def pause_resume(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not interaction.user.voice:
            await interaction.response.send_message("<:icons_Wrong:1449445703448264895> You are not in a voice channel.", ephemeral=True)
            return
        if interaction.user.voice.channel != interaction.guild.voice_client.channel:
            await interaction.response.send_message("<:stolen_emoji:1449451348759416912> You are not in the same voice channel.", ephemeral=True)
            return
        
        if self.player.is_playing and not self.player.paused:
            await self.player.set_pause(True)
            button.label = "Resume"
            await interaction.response.send_message("Music paused.", ephemeral=True)
        elif self.player.paused:
            await self.player.set_pause(False)
            button.label = "Pause"
            await interaction.response.send_message("Music resumed.", ephemeral=True)
        else:
            await interaction.response.send_message("No music is playing.", ephemeral=True)
        
        await interaction.message.edit(view=self)

    @discord.ui.button(label="Skip", style=discord.ButtonStyle.secondary)
    async def skip(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not interaction.user.voice:
            await interaction.response.send_message("<:stolen_emoji:1449451348759416912> You are not in a voice channel.", ephemeral=True)
            return
        if interaction.user.voice.channel != interaction.guild.voice_client.channel:
            await interaction.response.send_message("<:stolen_emoji:1449451348759416912> You are not in the same voice channel.", ephemeral=True)
            return
        
        if self.player.is_playing:
            try:
                await self.player.skip()
                embed = discord.Embed(color=0x2e2e2e, description="Skipped current track.")
                embed.set_author(name="Skipped the current playing track")
                await interaction.response.send_message(embed=embed, ephemeral=True)  # FIX: was missing response
            except Exception as e:
                embed = discord.Embed(description=f"An error occurred while skipping: {str(e)}")
                await interaction.response.send_message(embed=embed, ephemeral=True)
        else:
            await interaction.response.send_message("No music is playing.", ephemeral=True)

    @discord.ui.button(label="Shuffle", style=discord.ButtonStyle.secondary)
    async def shuffle(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not interaction.user.voice:
            await interaction.response.send_message("<:stolen_emoji:1449451348759416912> You are not in a voice channel.", ephemeral=True)
            return
        if not interaction.guild.voice_client:
            await interaction.response.send_message("<:stolen_emoji:1449451348759416912> I am not connected to any voice channel.", ephemeral=True)
            return
        if self.player.paused:
            await interaction.response.send_message("<:stolen_emoji:1449451348759416912> I am currently paused, please use `.resume`.", ephemeral=True)
            return
        if not self.player.is_playing:
            await interaction.response.send_message("No tracks in the queue.", ephemeral=True)
            return
        if interaction.user.voice.channel != interaction.guild.voice_client.channel:
            await interaction.response.send_message("<:stolen_emoji:1449451348759416912> You are not in the same voice channel.", ephemeral=True)
            return
        if not self.player.queue:
            await interaction.response.send_message("No queue to shuffle.", ephemeral=True)
            return
        
        self.player.queue.shuffle()
        await interaction.response.send_message("Shuffled the queue", ephemeral=True)

    @discord.ui.button(label="Stop", style=discord.ButtonStyle.secondary)
    async def stop(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not interaction.user.voice:
            await interaction.response.send_message("<:stolen_emoji:1449451348759416912> You are not in a voice channel.", ephemeral=True)
            return
        if interaction.user.voice.channel != interaction.guild.voice_client.channel:
            await interaction.response.send_message("<:stolen_emoji:1449451348759416912> You are not in the same voice channel.", ephemeral=True)
            return
        
        if self.player.is_playing:
            await self.player.stop()
            await interaction.guild.voice_client.disconnect()
            await interaction.response.send_message("Music stopped and bot disconnected.", ephemeral=True)
        else:
            await interaction.response.send_message("No music is playing.", ephemeral=True)

class Music(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.lavalink = bot.lavalink
        self.color = discord.Color.red()
        self.user_volumes = {}
        self.user_volume_range = {}
        self.user_sources = {}
        

    async def create_player(ctx: commands.Context):
        if ctx.guild is None:
            raise commands.NoPrivateMessage()

        player = ctx.bot.lavalink.player_manager.create(ctx.guild.id)

        should_connect = ctx.command.name in ('play',)

        voice_client = ctx.voice_client

        if not ctx.author.voice or not ctx.author.voice.channel:
            if voice_client is not None:
                raise commands.CommandInvokeError('You need to join my voice channel first.')
            raise commands.CommandInvokeError('Join a voicechannel first.')

        voice_channel = ctx.author.voice.channel

        if voice_client is None:
            if not should_connect:
                raise commands.CommandInvokeError("I'm not playing music.")

            permissions = voice_channel.permissions_for(ctx.me)

            if not permissions.connect or not permissions.speak:
                raise commands.CommandInvokeError('I need the `CONNECT` and `SPEAK` permissions.')

            if voice_channel.user_limit > 0:
                if len(voice_channel.members) >= voice_channel.user_limit and not ctx.me.guild_permissions.move_members:
                    raise commands.CommandInvokeError('Your voice channel is full!')

            player.store('channel', ctx.channel.id)
            await ctx.author.voice.channel.connect(cls=LavalinkVoiceClient, self_deaf=True)
        elif voice_client.channel.id != voice_channel.id:
            raise commands.CommandInvokeError('You need to be in my voicechannel.')

        return True

    async def voice_check(self, ctx: commands.Context):
        if not ctx.author.voice:
            emb = discord.Embed(
                description="You must be in a voice channel to use this command.",
                color=0x2e2e2e
            )
            await ctx.send(embed=emb)
            return False
        if not ctx.voice_client:
            emb = discord.Embed(
                description="I am not connected to a voice channel.",
                color=0x2e2e2e
            )
            await ctx.send(embed=emb)
            return False
        if ctx.author.voice.channel != ctx.voice_client.channel:
            emb = discord.Embed(
                description="You must be in the same voice channel as me to use this command.",
                color=0x2e2e2e
            )
            await ctx.send(embed=emb)
            return False
        return True
    
    @lavalink.listener(TrackStartEvent)
    async def on_track_start(self, event: TrackStartEvent):
        print("track event ready")
        guild_id = event.player.guild_id
        track = event.track
        player = event.player
        guild = self.bot.get_guild(int(player.guild_id))
        
        if not hasattr(player, 'last_track_id'):
            player.last_track_id = None
            
        if player.last_track_id == track.identifier:
            return
        
        player.last_track_id = track.identifier

        if player.home:
            duration = lavalink.utils.format_time(track.duration)
            requester = guild.get_member(track.requester)
            if requester is None:
                requester = await self.bot.fetch_user(track.requester)
                
            embed = discord.Embed()
            embed.set_author(name="Now Playing", icon_url=self.bot.user.avatar.url)
            embed.set_thumbnail(url=track.artwork_url)
            embed.color = discord.Color.dark_embed()
            embed.description = (
                f'- [{track.title} - {track.author}](https://discord.gg/kBTYHX6zcB)\n'
                f'- Duration: `{duration}`\n'
                f'- Requester: **[{requester.mention}]**'
                )
            try:
                if hasattr(player, 'msg'):
                    try:
                        await player.msg.delete()
                    except:
                        pass
                player.msg = await player.home.send(embed=embed, view=MusicControlView(player))
            except Exception as e:
                print(e)
        else:
            pass
        
        vc = guild.get_channel(player.channel_id)
        if vc and isinstance(vc, discord.VoiceChannel):
            try:
                status = f"**♬ {track.title[:90]}**  - {track.author}" 
                await asyncio.sleep(2)
                await vc.edit(status=status)
            except Exception as e:
                print(e)
        
    @lavalink.listener(QueueEndEvent)
    async def on_queue_end(self, event: QueueEndEvent):
        player = event.player
        if hasattr(player, 'repeat') and player.repeat:
            await player.play(event.track)
        if hasattr(player, 'ctx') and hasattr(player.ctx, 'msg'):
            try:
                await player.ctx.msg.delete()
            except:
                pass
        if hasattr(player, 'msg'):
            try:
                await player.msg.delete()
            except:
                pass
            
    @commands.hybrid_group(aliases=["tg"])
    async def toggle(self, ctx):
        embed1 = discord.Embed(title="Toggles Commands", color=discord.Color.blurple())
        embed1.description = "**.toggle**\n\n**Subcommands**\n`1.` **.toggle source** <:ecl_white_arrow:1449451303406403746> Sets default source of music ( for you )\n`2.` **.toggle volume** <:ecl_white_arrow:1449451303406403746> Sets default volume of music ( for you )"
        await ctx.send(embed=embed1)

    @toggle.command(name="volume", description="Toggles the volume for your music.")
    async def toggle_volume(self, ctx: commands.Context, vol: int):
        # Validate volume levels
        if not (0 <= vol <= 200):
            embed = discord.Embed(description="<:stolen_emoji:1449451348759416912> Please provide valid volume levels between 0 and 200.", colour=0x2e2e2e)
            return await ctx.reply(embed=embed, mention_author=False, delete_after=5)
        
        vc = ctx.voice_client

        # Get the user's volume range
        current_volume = self.user_volumes.get(ctx.author.id, vol)

        # Toggle between low and high volume
        new_volume = vol
        await vc.set_volume(new_volume)
        
        # Update the user's volume in the dictionary
        self.user_volumes[ctx.author.id] = new_volume

        embed = discord.Embed(description=f"<:Discord_voice_white:1449451318732128417> | Successfully set the volume to {new_volume}%.", colour=0x2e2e2e)
        await ctx.reply(embed=embed, mention_author=False)
            
    @toggle.command(name="source", description="Select the music source.")
    async def toggle_source(self, ctx):
        """Create a message with buttons to toggle music sources."""

        # Define the buttons
        youtube_button = Button(emoji="<:YouTube:1449451324621066242>", label="YouTube", style=discord.ButtonStyle.secondary, custom_id="source_youtube")
        jio_button = Button(emoji="<:Spotify:1449451331214508124> ", label="Spotify", style=discord.ButtonStyle.secondary, custom_id="source_spotify")
        soundcloud_button = Button(emoji="<:soundcloud:1449451337048916028>", label="SoundCloud", style=discord.ButtonStyle.secondary, custom_id="source_soundcloud")

        # Define the view with buttons
        view = View()
        view.add_item(youtube_button)
        view.add_item(jio_button)
        view.add_item(soundcloud_button)

        # Send the message with buttons
        embed = discord.Embed(color=self.color)
        embed.description = "**Select your Default Music Source**"
        await ctx.send(embed=embed, view=view)
        
    @commands.Cog.listener()
    async def on_interaction(self, interaction: discord.Interaction):
        """Handle button interactions."""
        if interaction.type == discord.InteractionType.component:
            user_id = interaction.user.id
            custom_id = interaction.data["custom_id"]
            
            if custom_id == "source_youtube":
                self.user_sources[user_id] = "ytsearch"
                await interaction.response.send_message("YouTube selected as your music source.", ephemeral=True)
            elif custom_id == "source_spotify":
                self.user_sources[user_id] = "spsearch"
                await interaction.response.send_message("Spotify selected as your music source.", ephemeral=True)
            elif custom_id == "source_soundcloud":
                self.user_sources[user_id] = "scsearch"
                await interaction.response.send_message("SoundCloud selected as your music source.", ephemeral=True)

    @commands.command(name="join")
    async def join(self, ctx: commands.Context):
        if ctx.voice_client:
            emb = discord.Embed(
                description="I am already connected to a voice channel.",
                color=0x2e2e2e
            )
            await ctx.send(embed=emb)
            return
        else:
            try:
                self.lavalink.player_manager.create(guild_id=ctx.guild.id)  
                await ctx.guild.change_voice_state(channel=ctx.author.voice.channel, self_mute=True, self_deaf=True)
                await ctx.author.voice.channel.connect(cls=LavalinkVoiceClient)
                player = self.lavalink.player_manager.create(guild_id=ctx.guild.id)
                player.home = ctx.channel
                await ctx.reply(
                    embed=discord.Embed(
                        description=f"Connected to {ctx.author.voice.channel.name}",
                        color=0x2e2e2e
                    )
                )
            except Exception as e:
                emb = discord.Embed(
                    description="An error occurred while trying to connect to the voice channel.",
                    color=0x2e2e2e
                )
                await ctx.send(embed=emb)
                print(e)

    @commands.hybrid_command(name="play")
    @commands.check(create_player)
    async def play(self, ctx: commands.Context, *, query: str):
        player = self.bot.lavalink.player_manager.get(ctx.guild.id)
        user_source = self.user_sources.get(ctx.author.id, "spsearch")

        if not hasattr(player, 'home'):
            player.home = ctx.channel

        if query.startswith("https://open.spotify.com"):
            source = None
        elif query.startswith('yt '):
            source = "ytsearch"
            query = query.replace("yt", "", 1).strip()
        elif query.startswith('sp '):
            source = "spsearch"
            query = query.replace("sp", "", 1).strip()
        else:
            source = user_source

        query = query.strip("<>")
        search_query = query if source is None else f"{source}:{query}"
        results = await player.node.get_tracks(search_query)

        # FIX: was outside the method (bad indentation) — moved inside
        if not results or results.load_type in (LoadType.EMPTY, LoadType.ERROR) or not results.tracks:
            return await ctx.send("Nothing found!")

        if results.load_type == LoadType.PLAYLIST:
            tracks = results.tracks
            for track in tracks:
                player.add(track=track, requester=ctx.author.id)
            embed = discord.Embed(
                title="Playlist Enqueued!",
                description=f"{results.playlist_info.name} - {len(tracks)} tracks",
                color=discord.Color.blurple()
            )
        else:
            track = results.tracks[0]
            player.add(track=track, requester=ctx.author.id)
            embed = discord.Embed(
                description="<a:pinkkitty:1449451266416578630> **Added track to the queue**",
                color=0x2e2e2e
            )
            embed.set_thumbnail(url=ctx.author.display_avatar.url)

        await ctx.send(embed=embed, delete_after=5)

        if not player.is_playing:
            await player.play()
            
    @commands.hybrid_command(help="Pause the current playing music")
    async def pause(self, ctx):
        player = self.bot.lavalink.player_manager.get(ctx.guild.id)
        
        if not player:
            return await ctx.reply("I'm not connected to a voice channel.", delete_after=5)
            
        if not player.is_playing:
            return await ctx.reply("I'm not playing anything.", delete_after=5)
            
        await player.set_pause(True)
        await ctx.reply("Paused the player.", delete_after=5)

    @commands.hybrid_command(help="Resume the current playing music")
    async def resume(self, ctx):
        player = self.bot.lavalink.player_manager.get(ctx.guild.id)
        
        if not player:
            return await ctx.reply("I'm not connected to a voice channel.", delete_after=5)
            
        if not player.paused:
            return await ctx.reply("I'm not paused.", delete_after=5)
            
        await player.set_pause(False)
        await ctx.reply("Resumed the player.", delete_after=5)

    @commands.hybrid_command(help="Stop the music and disconnect")
    async def stop(self, ctx):
        player = self.bot.lavalink.player_manager.get(ctx.guild.id)
        
        if not player:
            return await ctx.reply("I'm not connected to a voice channel.", delete_after=5)
            
        player.queue.clear()
        await player.stop()
        await ctx.voice_client.disconnect(force=True)
        await ctx.reply("Stopped the player and disconnected.", delete_after=5)

    @commands.hybrid_command(aliases=['vol'], help="Change the player volume")
    async def volume(self, ctx, volume: int):
        player = self.bot.lavalink.player_manager.get(ctx.guild.id)
        
        if not player:
            return await ctx.reply("I'm not connected to a voice channel.", delete_after=5)
            
        if not 0 <= volume <= 200:
            return await ctx.reply("Volume must be between 0 and 200.", delete_after=5)
            
        await player.set_volume(volume)
        await ctx.reply(f"Set volume to {volume}%", delete_after=5)

    @commands.hybrid_command(help="Skip the current track")
    async def skip(self, ctx):
        player = self.bot.lavalink.player_manager.get(ctx.guild.id)
        
        if not player:
            return await ctx.reply("I'm not connected to a voice channel.", delete_after=5)
            
        if not player.is_playing:
            return await ctx.reply("I'm not playing anything.", delete_after=5)
            
        current_track = player.current
        await player.skip()
        if player.queue:
            await ctx.reply(f"Skipped: {current_track.title}", delete_after=5)
        else:
            await ctx.reply("Skipped the current track. Queue is now empty.", delete_after=5)
        
    @commands.hybrid_command(aliases=['q'], help="Show the current queue")
    async def queue(self, ctx):
        player = self.bot.lavalink.player_manager.get(ctx.guild.id)
        
        if not player:
            return await ctx.reply("I'm not connected to a voice channel.", delete_after=5)
            
        if not player.current:
            return await ctx.reply("I'm not playing anything.", delete_after=5)
            
        view = QueueButtonView(player, self.color, ctx.author.id)
        await view.start(ctx)
        
    @commands.hybrid_command(aliases=['nowp'], help="Shows What's Playing", usage="nowplaying")
    @commands.cooldown(1, 5, commands.BucketType.user)
    async def nowplaying(self, ctx):
        player = self.bot.lavalink.player_manager.get(ctx.guild.id)
    
        current_track = player.current
        current_time = player.position  # Current time in milliseconds
        total_duration = current_track.duration  # Total duration in milliseconds

        # Generate the progress bar
        bar = progress_bar(current_time, total_duration)
        
        queue = enumerate(list(player.queue), start=1)
        requester = player.requester if hasattr(player, 'requester') else ctx.author
        author = current_track.author
        length_seconds = round(player.current.duration) / 1000
        hours, remainder = divmod(length_seconds, 3600)
        minutes, seconds = divmod(remainder, 60)
        duration_str = f"{int(hours):02d}:{int(minutes):02d}:{int(seconds):02d}"
        embed6 = discord.Embed(color=self.color)
        embed6.description = f"<:WickTick:1449451342794981548> **Now Playing - {player.current.title}**\n\n<:stolen_emoji:1449451348759416912> **Duration :** {duration_str}\n<:stolen_emoji:1449451348759416912> **Author :** {author}\n<:stolen_emoji:1449451348759416912> **Requested by :** {requester.display_name}\n<:emoji_20:1449451355964968980> **{bar}**"
        await ctx.reply(embed=embed6, mention_author=False)
        
    @commands.command(aliases=['dc', 'leave'])
    @commands.check(create_player)
    async def disconnect(self, ctx):
        player = self.bot.lavalink.player_manager.get(ctx.guild.id)

        player.queue.clear()
        await player.stop()
        await ctx.voice_client.disconnect(force=True)
        await ctx.send("Disconnected.")
        
    @commands.hybrid_command(aliases=['look'], help="Seek Into The Track", usage="seek <time>")
    @commands.cooldown(1, 5, commands.BucketType.user)
    async def seek(self, ctx, *, time_str):
        player = self.bot.lavalink.player_manager.get(ctx.guild.id)
        
        if not player:
            embed = discord.Embed(description="<:stolen_emoji:1449451348759416912> I am not connected to any voice channel.", colour=self.color)
            return await ctx.reply(embed=embed, mention_author=False)
        
        if not ctx.author.voice or not ctx.author.voice.channel:
            embed2 = discord.Embed(description="<:stolen_emoji:1449451348759416912> You are not in a voice channel.", colour=self.color)
            return await ctx.reply(embed=embed2, mention_author=False)
        if player.paused:
            embed3 = discord.Embed(description="<:stolen_emoji:1449451348759416912> I am currently paused, please use `.resume`.", colour=self.color)
            return await ctx.reply(embed=embed3, mention_author=False)
        if not player.current:
            embed4 = discord.Embed(description="<:stolen_emoji:1449451348759416912> I am not playing any song.", colour=self.color)
            return await ctx.reply(embed=embed4, mention_author=False)
        
        if ctx.author.voice.channel.id != int(player.channel_id):
            embed5 = discord.Embed(description="<:stolen_emoji:1449451348759416912> You are not in the same voice channel.", colour=self.color)
            return await ctx.reply(embed=embed5, mention_author=False)
        
        time_pattern = re.compile(r"(\d+:\d+|\d+)")
        match = time_pattern.match(time_str)
        
        if not match:
            embed6 = discord.Embed(description="<:stolen_emoji:1449451348759416912> Invalid time format. Please use either `mm:ss` or `ss`.", colour=self.color)
            return await ctx.reply(embed=embed6, mention_author=False)
        time_seconds = 0
        if match.group(1):
            time_components = list(map(int, match.group(1).split(":")))
            time_seconds = sum(c * 60 ** i for i, c in enumerate(reversed(time_components)))
            await player.seek(time_seconds * 1000)
            embed7 = discord.Embed(description=f"<:tickk:1449451362537701406> Successfully sought to {time_str}.", colour=self.color)
            await ctx.reply(embed=embed7, mention_author=False)
            
    @commands.command(name="enhance")
    @commands.bot_has_permissions(manage_channels=True)
    async def optimize_vc(self, ctx):
        player = self.bot.lavalink.player_manager.get(ctx.guild.id)
        
        if ctx.voice_client is None:
            embed = discord.Embed(description="<:stolen_emoji:1449451348759416912> I am not connected to any voice channel.", colour=self.color)
            return await ctx.reply(embed=embed, mention_author=False)      
        elif not getattr(ctx.author.voice, "channel", None):
            embed2 = discord.Embed(description="<:stolen_emoji:1449451348759416912> You are not in a voice channel.", colour=self.color)
            return await ctx.reply(embed=embed2, mention_author=False)               
        if ctx.author.voice.channel != ctx.voice_client.channel:
            embed5 = discord.Embed(description="<:stolen_emoji:1449451348759416912> You are not in the same voice channel.", colour=self.color)
            return await ctx.reply(embed=embed5, mention_author=False)
        
        author = ctx.author
        vc = author.voice.channel
        guild = ctx.guild
        max_bitrate = vc.guild.bitrate_limit
        premium_tier = guild.premium_tier
        max_bitrates = {
            0: 96000,
            1: 128000,
            2: 256000,
            3: 384000,
        }
        bitrate = max_bitrates.get(premium_tier, 64000)
        embed = discord.Embed(description=f"<a:MekoLoading:1449451273442299934> **Adjusting parameters for a richer and fuller sound !**")
        ok = await ctx.reply(embed=embed)
        try:
            await vc.edit(
                bitrate=bitrate, 
                rtc_region="india",
            )
            await player.set_volume(80)
            embed = discord.Embed(
                description=(
                    f"<a:H_TICK:1449446011490537603> Set voice channel bitrate to **{bitrate // 1000}** kbps\n"
                    f"<a:H_TICK:1449446011490537603> Set voice channel region to **India**\n"
                    f"<a:H_TICK:1449446011490537603> Set vol to **80%** to reduce distortions\n"
                    f"<a:H_TICK:1449446011490537603> Optimized audio for best listening experience."
                ),
                color=discord.Color.red()
            )
            embed.set_author(
                name=vc.guild.name,
                icon_url=vc.guild.icon.url if vc.guild.icon else None
            )
            await ok.edit(embed=embed)
        except discord.HTTPException as e:
            await ctx.send(f"Failed to optimize the voice channel. Error: {str(e)}") 
        
    @commands.hybrid_command(name="forcefix", description="Makes the bot leaves the vc forcefully")
    @commands.cooldown(1, 5, commands.BucketType.user)
    async def forcefix(self, ctx: commands.Context):
        try:
            await ctx.voice_client.disconnect()
            mbed = discord.Embed(color=self.color)
            mbed.set_footer(text=f"Successfully Fixed the current player.", icon_url=ctx.author.display_avatar.url)
            await ctx.send(embed=mbed)
        except:
            mbed = discord.Embed(color=self.color)
            mbed.set_footer(text=f"I am not connected to any of the voice channel.", icon_url=ctx.author.display_avatar.url)
            await ctx.send(embed=mbed)
            
    @commands.hybrid_command(name="repeat", aliases=["loop", "l"])
    @commands.cooldown(1, 5, commands.BucketType.user)
    async def repeat(self, ctx: commands.Context):
        """Toggles repeating the current track."""
        player: lavalink.DefaultPlayer = self.bot.lavalink.player_manager.get(ctx.guild.id)

        if not player or not player.is_playing:
            return await ctx.send("❌ Nothing is playing currently.")

        if player.loop == lavalink.DefaultPlayer.LOOP_SINGLE:
            player.loop = lavalink.DefaultPlayer.LOOP_NONE
            embed = discord.Embed(description=" Loop has been **disabled**.", color=discord.Color.red())
        else:
            player.loop = lavalink.DefaultPlayer.LOOP_SINGLE
            embed = discord.Embed(description=" Loop has been **enabled for current track**.", color=discord.Color.green())

        await ctx.send(embed=embed)


async def setup(bot: commands.Bot):
    cog = Music(bot)
    await bot.add_cog(cog)
    
    if bot.lavalink:
        bot.lavalink.add_event_hooks(cog)
