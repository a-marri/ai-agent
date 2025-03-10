import os
import discord
from discord.ext import commands
import random
from enum import Enum
from typing import Dict, List, Optional, Union
import asyncio
from dotenv import load_dotenv
import aiohttp
import json
from discord.ui import Select, View
import time

# Load environment variables
load_dotenv()

# Bot setup
intents = discord.Intents.default()
intents.message_content = True
intents.members = True
bot = commands.Bot(command_prefix='!', intents=intents)
bot.remove_command('help')  # Remove default help command

# Game constants
MIN_PLAYERS = 4  # Minimum number of players required to start a game (1 Mafia, 1 Detective, 1 Doctor, 1 Villager)

class GrokAgent:
    def __init__(self):
        self.api_key = os.getenv('GROK_API_KEY')
        if not self.api_key:
            raise ValueError("GROK_API_KEY environment variable is not set")
        self.api_url = "https://api.x.ai/v1/chat/completions"
        
    async def run(self, message) -> str:
        """Send a request to Grok API and get the response"""
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.api_key}"
        }
        
        data = {
            "messages": [
                {
                    "role": "system",
                    "content": "You are a creative storyteller for a Mafia game, skilled at creating brief, engaging narratives with dark humor."
                },
                {
                    "role": "user",
                    "content": message.content
                }
            ],
            "model": "grok-2-latest",
            "stream": False,
            "temperature": 0.7  # Add some creativity to the stories
        }
        
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(self.api_url, headers=headers, json=data) as response:
                    if response.status == 200:
                        result = await response.json()
                        return result['choices'][0]['message']['content']
                    else:
                        print(f"Error from Grok API: {response.status}")
                        return "The village continues its story..."  # Fallback message
        except Exception as e:
            print(f"Error calling Grok API: {e}")
            return "The village continues its story..."  # Fallback message

class StoryTeller:
    def __init__(self):
        self.agent = GrokAgent()
        
    async def generate_story(self, prompt: str, max_length: int = 1900) -> str:
        """Generate a story using Grok AI"""
        try:
            # Create a mock discord message for the agent
            mock_message = type('MockMessage', (), {'content': prompt})()
            response = await self.agent.run(mock_message)
            
            if not response:
                return "The village continues its story..."  # Fallback if empty response
            
            # If response is too long, ask Grok to shorten it
            if len(response) > max_length:
                shorten_prompt = f"Please shorten this story while keeping the main dramatic elements (keep it under {max_length} characters):\n\n{response}"
                mock_message.content = shorten_prompt
                response = await self.agent.run(mock_message)
                
                if not response or len(response) > max_length:
                    # If still too long or empty, use first max_length chars as fallback
                    return response[:max_length]
                    
            return response
                
        except Exception as e:
            print(f"Error generating story: {e}")
            return "The village continues its story..."  # Fallback message if anything goes wrong

class Role(Enum):
    VILLAGER = 1
    MAFIA = 2
    DETECTIVE = 3
    DOCTOR = 4

class GameState(Enum):
    WAITING = 1
    IN_PROGRESS = 2
    DAY = 3
    NIGHT = 4
    ENDED = 5
    VOTING = 6

class NPCPlayer:
    def __init__(self, name: str, player_id: int):
        self.name = name
        self.id = player_id
        
    async def send(self, message: str):
        # NPCs don't need to receive messages
        pass

class PollSelect(discord.ui.Select):
    def __init__(self, parent_view):
        self.parent_view = parent_view  # Store reference to parent view
        super().__init__(
            placeholder="Make your selection...",
            min_values=1,
            max_values=1,
            options=[],
        )

    async def callback(self, interaction: discord.Interaction):
        """Handle vote selection"""
        try:
            # Store the vote
            self.parent_view.votes[interaction.user.id] = int(self.values[0])
            
            # Acknowledge the vote
            await interaction.response.send_message(f"Your vote has been recorded!", ephemeral=True)
            
        except Exception as e:
            print(f"Error in poll callback: {e}")
            await interaction.response.send_message("There was an error recording your vote.", ephemeral=True)

class PollView(discord.ui.View):
    def __init__(self, timeout=30):
        super().__init__(timeout=timeout)
        self.votes = {}
        self.select = PollSelect(self)  # Create select with reference to this view
        self.add_item(self.select)  # Add select to view

    def get_votes(self):
        """Return the current votes"""
        return self.votes

    async def on_timeout(self):
        """Handle timeout of the poll"""
        for item in self.children:
            item.disabled = True

class KillView(View):
    def __init__(self, game, alive_players):
        super().__init__(timeout=45)
        self.game = game
        
        # Create the select menu
        select = Select(
            placeholder="Choose a player to kill...",
            options=[
                discord.SelectOption(
                    label=game.players[pid].display_name,
                    value=str(pid)
                ) for pid in alive_players
            ]
        )
        
        async def kill_callback(interaction):
            if interaction.user.id not in [pid for pid, role in game.player_roles.items() if role == Role.MAFIA]:
                await interaction.response.send_message("You are not authorized to make this selection!", ephemeral=True)
                return
                
            target_id = int(select.values[0])
            game.night_actions[interaction.user.id] = {
                'action': 'kill',
                'target': target_id
            }
            await interaction.response.send_message(f"You have chosen to kill {game.players[target_id].display_name}", ephemeral=True)
            self.stop()
            
        select.callback = kill_callback
        self.add_item(select)

class ProtectView(View):
    def __init__(self, game, alive_players):
        super().__init__(timeout=45)
        self.game = game
        
        select = Select(
            placeholder="Choose a player to protect...",
            options=[
                discord.SelectOption(
                    label=game.players[pid].display_name,
                    value=str(pid)
                ) for pid in alive_players
            ]
        )
        
        async def protect_callback(interaction):
            if interaction.user.id not in [pid for pid, role in game.player_roles.items() if role == Role.DOCTOR]:
                await interaction.response.send_message("You are not authorized to make this selection!", ephemeral=True)
                return
                
            target_id = int(select.values[0])
            game.night_actions[interaction.user.id] = {
                'action': 'protect',
                'target': target_id
            }
            await interaction.response.send_message(f"You have chosen to protect {game.players[target_id].display_name}", ephemeral=True)
            self.stop()
            
        select.callback = protect_callback
        self.add_item(select)

class InvestigateView(View):
    def __init__(self, game, alive_players):
        super().__init__(timeout=45)
        self.game = game
        
        select = Select(
            placeholder="Choose a player to investigate...",
            options=[
                discord.SelectOption(
                    label=game.players[pid].display_name,
                    value=str(pid)
                ) for pid in alive_players
            ]
        )
        
        async def investigate_callback(interaction):
            if interaction.user.id not in [pid for pid, role in game.player_roles.items() if role == Role.DETECTIVE]:
                await interaction.response.send_message("You are not authorized to make this selection!", ephemeral=True)
                return
                
            target_id = int(select.values[0])
            game.night_actions[interaction.user.id] = {
                'action': 'investigate',
                'target': target_id
            }
            await interaction.response.send_message(f"You have chosen to investigate {game.players[target_id].display_name}", ephemeral=True)
            self.stop()
            
        select.callback = investigate_callback
        self.add_item(select)

class VoteView(View):
    def __init__(self, game, alive_players):
        super().__init__(timeout=45)
        self.game = game
        
        select = Select(
            placeholder="Vote for who you think is the Mafia...",
            options=[
                discord.SelectOption(
                    label=game.players[pid].display_name,
                    value=str(pid)
                ) for pid in alive_players
            ]
        )
        
        async def vote_callback(interaction):
            if interaction.user.id not in game.alive_players:
                await interaction.response.send_message("Only alive players can vote!", ephemeral=True)
                return
                
            target_id = int(select.values[0])
            game.current_votes[interaction.user.id] = target_id
            
            # Send public vote message
            await game.main_channel.send(
                f"🗳️ {interaction.user.display_name} voted for {game.players[target_id].display_name}!"
            )
            
            # Acknowledge the vote to the user
            await interaction.response.send_message(
                f"Your vote has been recorded!", 
                ephemeral=True
            )
            
            try:
                # Update vote count message if it still exists
                if game.vote_message:
                    # Get vote counts
                    vote_counts = {}
                    for voted_id in game.current_votes.values():
                        vote_counts[voted_id] = vote_counts.get(voted_id, 0) + 1
                    
                    vote_status = "\n".join([
                        f"{game.players[pid].display_name}: {count} votes"
                        for pid, count in vote_counts.items()
                    ])
                    
                    if not vote_status:
                        vote_status = "No votes yet"
                    
                    try:
                        await game.vote_message.edit(content=f"Current Votes:\n{vote_status}")
                    except discord.NotFound:
                        # Message was deleted, clear the reference
                        game.vote_message = None
                    except Exception as e:
                        print(f"Error updating vote message: {e}")
            except Exception as e:
                print(f"Error in vote callback: {e}")
            
        select.callback = vote_callback
        self.add_item(select)

class MafiaGame:
    def __init__(self, guild, channel):
        self.guild = guild
        self.main_channel = channel
        self.players = {}
        self.player_roles = {}
        self.alive_players = []
        self.dead_players = []
        self.mafia_channel = None
        self.detective_channel = None
        self.doctor_channel = None
        self.night_actions = {}
        self.is_night = False
        self.active_polls = {}
        self.game_started = False
        self.state = GameState.WAITING
        self.current_votes = {}
        self.vote_message = None
        self.story_context = None  # Store the custom story context
        self.story_history = []    # Track the story progression
        self.storyteller = StoryTeller()  # Initialize the storyteller
        self.start_time = None  # Track when the game was created

    async def timeout_game(self, reason: str):
        """Handle game timeout"""
        await self.main_channel.send(f"⏰ {reason}")
        await self.cleanup_channels()
        self.reset_game_state()
        # Remove the game from active games
        if self.guild.id in active_games:
            del active_games[self.guild.id]

    async def check_start_timeout(self):
        """Check if game has timed out before starting"""
        return False

    async def check_vote_timeout(self):
        """Check if game should timeout due to inactivity"""
        return False

    async def set_story_context(self, context: str):
        """Set the story context for the game"""
        self.story_context = context
        self.story_history = []  # Reset story history when setting new context
        
        # Generate initial story setup
        setup_prompt = (
            f"Using this story context: {context}\n"
            "Create a very brief 2-3 sentence introduction to set up the story of this village. "
            "This will be the beginning of an ongoing narrative."
        )
        initial_story = await self.storyteller.generate_story(setup_prompt)
        self.story_history.append(initial_story)
        
        await self.main_channel.send(
            "Story context has been set! Here's how our tale begins:\n\n" + initial_story
        )

    async def generate_story_with_context(self, event_type: str, **kwargs) -> str:
        """Generate a story based on the event type and context"""
        # Get the last 2 story elements for continuity
        recent_history = self.story_history[-2:] if self.story_history else []
        history_context = "\n".join(recent_history)
        
        base_prompt = (
            f"Using this story context: {self.story_context}\n\n"
            f"Previous events:\n{history_context}\n\n"
            "Write a brief, punchy story (1-2 sentences max) with a touch of dark humor about "
        )

        if event_type == "death":
            victim = kwargs.get('victim')
            base_prompt += f"how {victim} met their unfortunate end. Focus on the funny or ironic way they died."
        elif event_type == "save":
            saved = kwargs.get('saved')
            base_prompt += f"how {saved} hilariously escaped death by pure luck or coincidence."
        elif event_type == "morning":
            base_prompt += "the village waking up to discover what happened during the night. Make it snappy and amusing."
        elif event_type == "night":
            base_prompt += "night falling over the village. Keep it brief but ominous with a touch of humor."
        elif event_type == "vote":
            victim = kwargs.get('victim')
            base_prompt += f"how the village decided to execute {victim}. Make their demise ironically funny."

        story = await self.storyteller.generate_story(base_prompt)
        self.story_history.append(story)
        return story

    async def begin_game(self):
        """Start the game and assign roles to players"""
        if len(self.players) < MIN_PLAYERS:
            return await self.main_channel.send(f"Not enough players to start the game. Minimum required: {MIN_PLAYERS}")
        
        # Assign roles first
        self.assign_roles()
        
        # Create role channels after roles are assigned
        await self.create_role_channels()
        
        # Send role information to players
        for player_id, role in self.player_roles.items():
            player = self.players[player_id]
            await player.send(f"Your role is: {role.name}")
        
        self.game_started = True
        self.state = GameState.IN_PROGRESS
        self.alive_players = list(self.players.keys())
        
        # Announce game start
        await self.main_channel.send("The game has begun! Check your DMs for your role.")
        await self.start_night()

    def generate_npc_name(self) -> str:
        """Generate a random medieval-style name for an NPC"""
        first_names = ["Aldrich", "Bartholomew", "Constantine", "Darius", "Edmund", "Felix", "Galahad", "Henrik"]
        surnames = ["Blackwood", "Crowley", "Darkshire", "Elderworth", "Frostweaver", "Grimsworth", "Hawthorne"]
        return f"{random.choice(first_names)} {random.choice(surnames)}"

    async def add_npcs_if_needed(self):
        """Add NPC players if there aren't enough real players"""
        min_players = 4  # Minimum required for a balanced game
        real_players = len([p for p in self.players.keys() if p < self.npc_base_id])
        print(f"Adding NPCs. Current real players: {real_players}")
        
        while len(self.players) < min_players:
            npc_id = self.npc_base_id + self.npc_count
            npc_name = self.generate_npc_name()
            npc = NPCPlayer(npc_name, npc_id)
            self.players[npc_id] = npc
            self.npc_count += 1
            print(f"Added NPC: {npc_name} (ID: {npc_id})")
            
            if self.main_channel:
                try:
                    if self.storyteller:
                        # Generate introduction story for NPC
                        prompt = f"Create a brief one-sentence introduction for {npc_name}, a stranger who has joined the village."
                        story = await self.storyteller.generate_story(prompt)
                        await self.main_channel.send(story)
                    else:
                        await self.main_channel.send(f"{npc_name} has joined the village.")
                except Exception as e:
                    print(f"Error introducing NPC: {e}")
                    await self.main_channel.send(f"{npc_name} has joined the village.")
                    
    def assign_roles(self):
        """Assign roles to all players randomly"""
        print("Assigning roles to players...")
        
        # Get list of all players
        player_ids = list(self.players.keys())
        if len(player_ids) < 4:
            raise ValueError("Not enough players to assign roles")
            
        # Calculate number of each role based on player count
        num_players = len(player_ids)
        num_mafia = max(1, num_players // 4)  # At least 1 mafia
        
        # Create list of all roles
        roles = []
        roles.extend([Role.MAFIA] * num_mafia)  # Add mafia roles
        roles.append(Role.DETECTIVE)  # Add one detective
        roles.append(Role.DOCTOR)  # Add one doctor
        
        # Fill remaining slots with villagers
        num_villagers = num_players - len(roles)
        roles.extend([Role.VILLAGER] * num_villagers)
        
        # Shuffle both the player IDs and roles
        random.shuffle(player_ids)
        random.shuffle(roles)
        
        # Reset roles and assign new ones
        self.player_roles.clear()
        for player_id, role in zip(player_ids, roles):
            self.player_roles[player_id] = role
            player_name = self.players[player_id].name
            print(f"Assigned {role.value} to {player_name}")
        
        print("Player Roles:")
        for player_id, role in self.player_roles.items():
            player_name = self.players[player_id].name
            print(f"{player_name}: {role.value}")
        
        # Set initial alive players
        self.alive_players = player_ids.copy()
        self.dead_players = []

    async def send_role_dms(self):
        """Send role information to all players"""
        print("Sending role DMs...")
        
        # First, collect all mafia members for the mafia message
        mafia_members = [self.players[pid].name for pid, role in self.player_roles.items() 
                        if role == Role.MAFIA and pid < self.npc_base_id]
        
        for player_id, role in self.player_roles.items():
            if player_id >= self.npc_base_id:  # Skip NPCs
                continue
                
            try:
                player = self.players[player_id]
                role_msg = f"Your role is: {role.value}"
                
                # Add mafia member list for mafia players
                if role == Role.MAFIA and len(mafia_members) > 1:
                    other_mafia = [name for name in mafia_members if name != player.name]
                    if other_mafia:
                        role_msg += f"\nOther mafia members: {', '.join(other_mafia)}"
                
                await player.send(role_msg)
                print(f"Sent role DM to {player_id}")
            except Exception as e:
                print(f"Error sending role DM to {player_id}: {e}")
                
    async def handle_action_vote(self, player_id: int, action_type: str, target_id: int):
        """Handle a vote for a night action"""
        try:
            if action_type in ['kill', 'investigate', 'save']:
                # Create a new entry for this player if it doesn't exist
                if player_id not in self.night_actions:
                    self.night_actions[player_id] = {}

                # Store the action and target
                self.night_actions[player_id] = {
                    'action': action_type,
                    'target': target_id
                }
                
                # Debug prints
                voter = self.players[player_id].name
                target = self.players[target_id].name
                print(f"DEBUG - Vote registered: {voter} ({self.player_roles[player_id].value}) voted to {action_type} {target}")
                return True
            return False
        except Exception as e:
            print(f"ERROR in handle_action_vote: {e}")
            print(f"player_id: {player_id}, action_type: {action_type}, target_id: {target_id}")
            return False

    async def process_votes(self, poll_id):
        """Process votes from a poll"""
        try:
            if poll_id not in self.active_polls:
                print(f"ERROR: Poll {poll_id} not found in active polls")
                return

            view = self.active_polls[poll_id]
            votes = view.get_votes()
            
            if not votes:
                await self.main_channel.send("No votes were cast!")
                return

            # Count votes
            vote_counts = {}
            for voter_id, target_id in votes.items():
                vote_counts[target_id] = vote_counts.get(target_id, 0) + 1

            # Find player(s) with most votes
            max_votes = max(vote_counts.values())
            potential_targets = [pid for pid, count in vote_counts.items() if count == max_votes]
            
            # Randomly choose from tied players
            eliminated_id = random.choice(potential_targets)
            eliminated_player = self.players[eliminated_id]
            
            # Remove player from alive list
            if eliminated_id in self.alive_players:
                self.alive_players.remove(eliminated_id)
                self.dead_players.append(eliminated_id)
                await self.main_channel.send(
                    f"The town has spoken! **{eliminated_player.name}** has been eliminated. "
                    f"They were a **{self.player_roles[eliminated_id].value}**."
                )
            
            # Clear the poll
            del self.active_polls[poll_id]
            
            # Check win conditions
            await self.check_win_conditions()
            
        except Exception as e:
            print(f"ERROR in process_votes: {e}")

    async def process_night_actions(self):
        """Process all night actions and determine outcomes"""
        try:
            if not self.night_actions:
                print("DEBUG - No night actions recorded")
                await self.main_channel.send("No actions were taken during the night.")
                return

            print("\nDEBUG - Processing night actions:")
            print(f"Current night actions: {self.night_actions}")

            # Process detective's investigation first
            detective_actions = []
            for pid, action_data in self.night_actions.items():
                if action_data['action'] == 'investigate':
                    detective_actions.append((pid, action_data['target']))
                    
            if detective_actions:
                _, target_id = detective_actions[0]
                detective_id = detective_actions[0][0]
                target_role = self.player_roles[target_id]
                target_player = self.players[target_id]
                print(f"DEBUG - Detective {self.players[detective_id].name} investigated {target_player.name}")
                if self.detective_channel:
                    await self.detective_channel.send(f"🔍 Investigation results: **{target_player.name}** is a **{target_role.value}**!")

            # Process doctor's save
            doctor_actions = []
            for pid, action_data in self.night_actions.items():
                if action_data['action'] == 'save':
                    doctor_actions.append((pid, action_data['target']))
                    
            saved_id = doctor_actions[0][1] if doctor_actions else None
            if doctor_actions:
                doctor_id = doctor_actions[0][0]
                print(f"DEBUG - Doctor {self.players[doctor_id].name} protected {self.players[saved_id].name}")
            else:
                print("DEBUG - No doctor action")

            # Process mafia's kill last
            mafia_actions = []
            for pid, action_data in self.night_actions.items():
                if action_data['action'] == 'kill':
                    mafia_actions.append((pid, action_data['target']))

            if mafia_actions:
                print("\nDEBUG - Mafia votes:")
                vote_counts = {}
                for pid, target_id in mafia_actions:
                    vote_counts[target_id] = vote_counts.get(target_id, 0) + 1
                    print(f"Mafia member {self.players[pid].name} voted to kill {self.players[target_id].name}")
                
                print(f"\nDEBUG - Vote counts:")
                for target_id, count in vote_counts.items():
                    print(f"{self.players[target_id].name}: {count} votes")
                
                max_votes = max(vote_counts.values())
                potential_targets = [pid for pid, count in vote_counts.items() if count == max_votes]
                target_id = random.choice(potential_targets)
                
                target_player = self.players[target_id]
                print(f"\nDEBUG - Final target selected: {target_player.name}")
                print(f"DEBUG - Doctor saved ID: {saved_id}")
                print(f"DEBUG - Target ID: {target_id}")
                
                if str(target_id) == str(saved_id):  # Convert both to strings for comparison
                    print("DEBUG - Target was saved by doctor!")
                    await self.main_channel.send(f"🏥 The Doctor successfully saved someone from death!")
                else:
                    if target_id in self.alive_players:
                        self.alive_players.remove(target_id)
                        self.dead_players.append(target_id)
                        print(f"DEBUG - {target_player.name} was killed")
                        await self.main_channel.send(f"💀 **{target_player.name}** was found dead! They were a **{self.player_roles[target_id].value}**.")
                    else:
                        print(f"ERROR - Target {target_id} not in alive_players")

            # Clear night actions
            self.night_actions.clear()
            
            # Check win conditions
            await self.check_win_conditions()
            
        except Exception as e:
            print(f"ERROR in process_night_actions: {e}")
            print(f"Night actions at time of error: {self.night_actions}")

    async def start_day(self):
        """Start the day phase where players vote on who to eliminate"""
        if await self.check_game_over():
            return

        self.state = GameState.DAY
        self.current_votes.clear()
        
        # Create and send the voting view
        view = VoteView(self, self.alive_players)
        try:
            self.vote_message = await self.main_channel.send("Current Votes:\nNo votes yet")
            voting_prompt = await self.main_channel.send("Choose who to vote out:", view=view)
            
            # Wait for votes (up to 45 seconds)
            start_time = asyncio.get_event_loop().time()
            while asyncio.get_event_loop().time() - start_time < 45:
                # Check if everyone has voted
                if len(self.current_votes) >= len(self.alive_players):
                    await self.main_channel.send("Everyone has voted! Moving on...")
                    break
                await asyncio.sleep(1)
            
            # Stop the view
            view.stop()
            
            # Try to delete voting messages
            try:
                if self.vote_message:
                    await self.vote_message.delete()
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                pass
                
            try:
                await voting_prompt.delete()
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                pass
                
        except Exception as e:
            print(f"Error in voting process: {e}")
            # Continue with the game even if there was an error
            pass
            
        # Count votes and eliminate player
        if self.current_votes:
            vote_counts = {}
            for voted_id in self.current_votes.values():
                vote_counts[voted_id] = vote_counts.get(voted_id, 0) + 1
            
            # Find player(s) with most votes
            max_votes = max(vote_counts.values())
            eliminated = [pid for pid, count in vote_counts.items() if count == max_votes]
            
            if len(eliminated) == 1:
                eliminated_id = eliminated[0]
                eliminated_player = self.players[eliminated_id]
                eliminated_role = self.player_roles[eliminated_id]
                
                self.alive_players.remove(eliminated_id)
                self.dead_players.append(eliminated_id)
                
                # Simple elimination message instead of story
                await self.main_channel.send(
                    f"The village has voted to eliminate **{eliminated_player.display_name}**.\n"
                    f"They were a **{eliminated_role.name}**!"
                )
            else:
                await self.main_channel.send(
                    "There was a tie in the voting! No one was eliminated."
                )
        else:
            await self.main_channel.send(
                "No one voted! No one was eliminated."
            )

        if await self.check_game_over():
            return
            
        await self.start_night()

    async def start_night(self):
        """Start the night phase of the game"""
        if await self.check_game_over():
            return

        self.state = GameState.NIGHT
        self.night_actions.clear()
        
        # Only generate night story for the first night
        if len(self.story_history) <= 1:  # Only initial story exists
            night_story = await self.generate_story_with_context("night")
            await self.main_channel.send(f"🌙 Night falls on the village...\n\n{night_story}\n\nAll players check your role channels for actions. You have 45 seconds!")
        else:
            # Simple message for subsequent nights
            await self.main_channel.send("🌙 Night falls on the village... All players check your role channels for actions. You have 45 seconds!")
        
        # Track all voting messages to delete later
        voting_messages = []
        views_sent = []

        try:
            # Get counts of special roles that need to act
            mafia_members = [self.players[pid] for pid, role in self.player_roles.items() 
                            if role == Role.MAFIA and pid in self.alive_players]
            detective_members = [self.players[pid] for pid, role in self.player_roles.items() 
                               if role == Role.DETECTIVE and pid in self.alive_players]
            doctor_members = [self.players[pid] for pid, role in self.player_roles.items() 
                            if role == Role.DOCTOR and pid in self.alive_players]

            # Calculate total expected actions
            expected_actions = len(mafia_members) + len(detective_members) + len(doctor_members)

            # Send the views to each role channel
            if mafia_members and self.mafia_channel:
                alive_targets = [pid for pid in self.alive_players 
                               if pid not in [m.id for m in mafia_members]]
                if alive_targets:
                    view = KillView(self, alive_targets)
                    message = await self.mafia_channel.send("🔪 Choose your target to kill:", view=view)
                    views_sent.append(view)
                    voting_messages.append(message)

            if detective_members and self.detective_channel:
                alive_targets = [pid for pid in self.alive_players 
                               if pid != detective_members[0].id]
                if alive_targets:
                    view = InvestigateView(self, alive_targets)
                    message = await self.detective_channel.send("🔍 Choose a player to investigate:", view=view)
                    views_sent.append(view)
                    voting_messages.append(message)

            if doctor_members and self.doctor_channel:
                view = ProtectView(self, self.alive_players)
                message = await self.doctor_channel.send("💉 Choose a player to protect:", view=view)
                views_sent.append(view)
                voting_messages.append(message)

            # Wait for actions or timeout
            start_time = asyncio.get_event_loop().time()
            while asyncio.get_event_loop().time() - start_time < 45:
                # Check if all expected actions have been received
                if len(self.night_actions) >= expected_actions:
                    print("All night actions received, ending night phase early")
                    break
                await asyncio.sleep(1)

            # Stop all views
            for view in views_sent:
                view.stop()

            # Try to delete messages, but don't fail if we can't
            for message in voting_messages:
                try:
                    await message.delete()
                except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                    pass  # Ignore any errors when deleting messages

        except Exception as e:
            print(f"Error in start_night: {e}")
            pass

        # Process night actions
        await self.end_night()

    async def end_night(self):
        """Process all night actions and transition to day phase"""
        # Process night actions in order: Doctor -> Detective -> Mafia
        protected_player = None
        killed_player = None
        
        # Process doctor's protection
        for player_id, action in self.night_actions.items():
            if self.player_roles[player_id] == Role.DOCTOR:
                protected_player = action['target']
                break
        
        # Process mafia's kill
        for player_id, action in self.night_actions.items():
            if self.player_roles[player_id] == Role.MAFIA:
                if action['target'] != protected_player:
                    killed_player = action['target']
                break
        
        # Generate morning story
        morning_story = await self.generate_story_with_context("morning")
        await self.main_channel.send(f"☀️ {morning_story}")
        
        if killed_player:
            player = self.players[killed_player]
            self.alive_players.remove(killed_player)
            self.dead_players.append(killed_player)
            
            # Generate death story
            death_story = await self.generate_story_with_context("death", victim=player.display_name)
            await self.main_channel.send(death_story)
        elif protected_player:
            player = self.players[protected_player]
            # Generate save story
            save_story = await self.generate_story_with_context("save", saved=player.display_name)
            await self.main_channel.send(save_story)
        else:
            await self.main_channel.send("😌 Nobody died during the night.")
        
        # Process detective's investigation (only send to detective)
        for player_id, action in self.night_actions.items():
            if self.player_roles[player_id] == Role.DETECTIVE:
                target_id = action['target']
                target_role = self.player_roles[target_id]
                detective = self.players[player_id]
                await self.detective_channel.send(
                    f"🔍 Your investigation reveals that {self.players[target_id].display_name} "
                    f"is a {target_role.name}!"
                )
                break
        
        # Start day phase
        await self.start_day()

    async def start_voting_phase(self):
        """Start the voting phase during the day"""
        try:
            # Get list of alive players
            alive_players = [self.players[pid] for pid in self.alive_players]
            
            # Create the voting poll
            poll_msg = await self.create_action_poll(
                channel=self.main_channel,
                action_type="eliminate",
                alive_players=alive_players,
                timeout=30
            )
            
            if not poll_msg:
                print("ERROR: Failed to create voting poll")
                return
                
            # Wait for votes
            await asyncio.sleep(30)
            
            # Process votes
            await self.process_votes(poll_msg.id)
            
        except Exception as e:
            print(f"ERROR in start_voting_phase: {e}")

    async def eliminate_player(self, player_id: int):
        """Eliminate a player from the game"""
        if player_id in self.alive_players:
            self.alive_players.remove(player_id)
            player = self.players[player_id]
            role = self.player_roles.get(player_id, "Villager")
            
            # Announce elimination
            await self.main_channel.send(
                f"🪦 The village has decided to eliminate **{player.display_name}**.\n"
                f"They were a **{role}**!"
            )
            
            # Check win conditions
            await self.check_win_condition()
            
    def get_player_status_message(self):
        """Get a formatted message showing alive and dead players"""
        alive_players = [self.players[pid].name for pid in self.alive_players]
        dead_players = [self.players[pid].name for pid in self.players.keys() if pid not in self.alive_players]
        
        msg = "**Player Status**\n"
        msg += "🟢 **Alive**: " + ", ".join(alive_players) + "\n"
        if dead_players:
            msg += "💀 **Dead**: " + ", ".join(dead_players)
        return msg

    async def check_win_conditions(self) -> bool:
        """Check if either faction has won"""
        mafia_count = sum(1 for pid in self.alive_players if self.player_roles[pid] == Role.MAFIA)
        villager_count = len(self.alive_players) - mafia_count
        
        if mafia_count == 0:
            # Village wins
            prompt = "Create an triumphant ending where the village successfully eliminated all mafia members and peace is restored."
            story = await self.storyteller.generate_story(prompt)
            await self.main_channel.send(f"{story}\n\n The Village has won! ")
            return True
        elif mafia_count >= villager_count:
            # Mafia wins
            prompt = "Create a dark ending where the mafia has gained control of the village, striking fear into the hearts of the remaining villagers."
            story = await self.storyteller.generate_story(prompt)
            await self.main_channel.send(f"{story}\n\n The Mafia has won! ")
            return True
        return False

    async def reset_game(self):
        """Reset the game state and clean up resources"""
        # Clean up mafia chat channel if it exists
        if self.mafia_channel:
            try:
                await self.mafia_channel.delete()
            except:
                pass
            self.mafia_channel = None
        
        # Reset all game state
        self.players.clear()
        self.player_roles.clear()
        self.dead_players = set()
        self.night_actions = {}
        self.votes = {}
        self.state = GameState.WAITING
        self.main_channel = None
        self.mafia_channel = None
        self.detective_channel = None
        self.doctor_channel = None
        self.villager_channel = None
        self.npc_base_id = 1000000

    async def remove_player(self, player_id: int) -> bool:
        """Remove a player from the game and handle necessary adjustments"""
        if player_id not in self.players:
            return False
            
        player = self.players[player_id]
        
        # Remove from all game states
        self.players.pop(player_id)
        self.player_roles.pop(player_id, None)
        if player_id in self.alive_players:
            self.alive_players.remove(player_id)
        if player_id in self.dead_players:
            self.dead_players.remove(player_id)
        self.current_votes = {k: v for k, v in self.current_votes.items() if k != player_id and v != player_id}
        self.night_actions = {k: v for k, v in self.night_actions.items() if k != player_id and v != player_id}
        
        # Remove from mafia chat if applicable
        if self.mafia_channel and self.player_roles.get(player_id) == Role.MAFIA:
            try:
                await self.mafia_channel.set_permissions(player, overwrite=None)
            except:
                pass
        
        # Generate quit story
        if self.state != GameState.WAITING:
            prompt = f"Create a dramatic description of {player.name}'s sudden and mysterious departure from the village."
            story = await self.storyteller.generate_story(prompt)
            if self.main_channel:
                await self.main_channel.send(story)
        
        # Check if game should end due to player count
        if self.state != GameState.WAITING and len(self.players) < 4:
            await self.main_channel.send("Not enough players remaining. The game must end.")
            await self.reset_game()
            return True
            
        # Check win conditions if game is in progress
        if self.state != GameState.WAITING:
            await self.check_win_conditions()
            
        return True

    async def get_npc_action(self, npc_id: int, action_type: str) -> Optional[int]:
        """Get an AI-generated action for an NPC"""
        if not self.storyteller:
            return random.choice([pid for pid in self.alive_players if pid != npc_id])
            
        npc = self.players[npc_id]
        npc_role = self.player_roles[npc_id]
        
        # Get list of valid targets
        targets = [pid for pid in self.alive_players if pid != npc_id]
        if not targets:
            return None
            
        target_names = [self.players[pid].name for pid in targets]
        
        # Create context for the AI
        context = f"You are {npc.name}, a {npc_role.value} in a game of Mafia. "
        
        if action_type == "vote":
            context += f"You must vote to eliminate one player who you suspect is in the mafia. The candidates are: {', '.join(target_names)}."
        elif action_type == "mafia_kill":
            context += f"As a mafia member, you must choose a villager to eliminate. The potential targets are: {', '.join(target_names)}."
        elif action_type == "investigate":
            context += f"As the detective, you must choose a player to investigate. The suspects are: {', '.join(target_names)}."
        elif action_type == "protect":
            context += f"As the doctor, you must choose a player to protect from the mafia. The potential targets are: {', '.join(target_names)}."
            
        try:
            # Create a mock discord message
            mock_message = type('MockMessage', (), {'content': context})()
            chosen_name = await self.storyteller.agent.run(mock_message)
            chosen_name = chosen_name.strip()
            
            # Find the closest matching player name
            best_match = None
            for pid in targets:
                if self.players[pid].name.lower() in chosen_name.lower():
                    best_match = pid
                    break
                    
            if best_match is not None:
                return best_match
            
            # Fallback to random choice if no match found
            return random.choice(targets) if targets else None
            
        except Exception as e:
            print(f"Error getting NPC action: {e}")
            # Return a random target as fallback
            return random.choice(targets) if targets else None

    async def process_npc_actions(self):
        """Process actions for all NPCs during appropriate game phases"""
        if self.state == GameState.NIGHT:
            for player_id, role in self.player_roles.items():
                if player_id >= self.npc_base_id and player_id in self.alive_players:
                    if role == Role.MAFIA:
                        target = await self.get_npc_action(player_id, "mafia_kill")
                        if target:
                            self.night_actions[player_id] = target
                    elif role == Role.DETECTIVE:
                        target = await self.get_npc_action(player_id, "investigate")
                        if target:
                            self.night_actions[player_id] = target
                    elif role == Role.DOCTOR:
                        target = await self.get_npc_action(player_id, "protect")
                        if target:
                            self.night_actions[player_id] = target
        
        elif self.state == GameState.VOTING:
            for player_id in self.alive_players:
                if player_id >= self.npc_base_id and player_id not in self.current_votes:
                    target = await self.get_npc_action(player_id, "vote")
                    if target:
                        self.current_votes[player_id] = target
                        
                        # Generate voting story for NPC
                        npc = self.players[player_id]
                        target_player = self.players[target]
                        prompt = f"Create a dramatic moment where {npc.name} accuses {target_player.name} of being in league with the mafia."
                        story = await self.storyteller.generate_story(prompt)
                        await self.main_channel.send(story)

    async def setup_channels(self):
        """Set up game channels"""
        try:
            # Create category for game channels
            category = await self.guild.create_category("Mafia Game")
            
            # Create role-specific channels
            self.mafia_channel = await self.guild.create_text_channel(
                'mafia-chat',
                category=category
            )
            
            self.detective_channel = await self.guild.create_text_channel(
                'detective-chat',
                category=category
            )
            
            self.doctor_channel = await self.guild.create_text_channel(
                'doctor-chat',
                category=category
            )
            
            # Set default permissions (deny access to everyone)
            for channel in [self.mafia_channel, self.detective_channel, self.doctor_channel]:
                await channel.set_permissions(self.guild.default_role, read_messages=False, send_messages=False)
            
            print("DEBUG - Channels created successfully")
            
        except Exception as e:
            print(f"ERROR in setup_channels: {e}")

    async def assign_channel_permissions(self):
        """Assign channel permissions based on roles"""
        try:
            for player_id, role in self.player_roles.items():
                player = self.players[player_id]
                
                if role == Role.MAFIA:
                    await self.mafia_channel.set_permissions(player, read_messages=True, send_messages=True)
                    print(f"DEBUG - Gave mafia access to {player.name}")
                elif role == Role.DETECTIVE:
                    await self.detective_channel.set_permissions(player, read_messages=True, send_messages=True)
                    print(f"DEBUG - Gave detective access to {player.name}")
                elif role == Role.DOCTOR:
                    await self.doctor_channel.set_permissions(player, read_messages=True, send_messages=True)
                    print(f"DEBUG - Gave doctor access to {player.name}")
                    
            print("DEBUG - Channel permissions assigned successfully")
            
        except Exception as e:
            print(f"ERROR in assign_channel_permissions: {e}")

    async def cleanup_channels(self):
        """Clean up game channels"""
        try:
            channels_to_delete = [
                self.mafia_channel,
                self.detective_channel,
                self.doctor_channel
            ]
            
            for channel in channels_to_delete:
                if channel:
                    await channel.delete()
            
            # Also delete the category if it exists
            if self.mafia_channel and self.mafia_channel.category:
                await self.mafia_channel.category.delete()
                
            print("DEBUG - Channels cleaned up successfully")
            
        except Exception as e:
            print(f"ERROR in cleanup_channels: {e}")

    def reset_game_state(self):
        """Reset all game state variables"""
        self.players.clear()
        self.player_roles.clear()
        self.alive_players.clear()
        self.dead_players.clear()
        self.night_actions.clear()
        self.active_polls.clear()
        self.current_votes.clear()
        self.game_started = False
        self.state = GameState.WAITING
        self.story_context = None
        self.story_history = []

    async def day_phase(self):
        """Start the day phase of the game"""
        self.state = GameState.DAY
        
        # Generate day story
        day_story = await self.storyteller.generate_story(self.story_prompts['day'])
        
        # Get status update
        status_msg = self.get_player_status_message()
        
        # Send day phase message
        await self.main_channel.send(
            f"☀️ **Day Phase Begins** ☀️\n\n"
            f"{day_story}\n\n"
            f"The village awakens to discuss the night's events...\n\n"
            f"{status_msg}"
        )
        
        # Start voting phase after discussion period
        await self.main_channel.send("The village will have 60 seconds for discussion before voting begins...")
        await asyncio.sleep(60)  # 60 seconds for discussion
        
        if self.state == GameState.DAY:  # Only proceed if still in day phase
            await self.start_voting_phase()

    async def create_role_channels(self):
        """Create private channels for special roles"""
        # Create category for mafia game channels if it doesn't exist
        category = await self.guild.create_category("Mafia Game", overwrites={
            self.guild.default_role: discord.PermissionOverwrite(read_messages=False)
        })
        
        # Create mafia channel
        self.mafia_channel = await self.guild.create_text_channel('mafia-chat', category=category)
        # Create detective channel
        self.detective_channel = await self.guild.create_text_channel('detective-chat', category=category)
        # Create doctor channel
        self.doctor_channel = await self.guild.create_text_channel('doctor-chat', category=category)
        
        # Set permissions for each channel
        for player_id, role in self.player_roles.items():
            player = self.guild.get_member(player_id)
            if role == Role.MAFIA:
                await self.mafia_channel.set_permissions(player, read_messages=True, send_messages=True)
            elif role == Role.DETECTIVE:
                await self.detective_channel.set_permissions(player, read_messages=True, send_messages=True)
            elif role == Role.DOCTOR:
                await self.doctor_channel.set_permissions(player, read_messages=True, send_messages=True)

    async def handle_kill_command(self, ctx, target_name):
        """Handle the kill command from mafia members"""
        if self.state != GameState.NIGHT:
            return await ctx.send("You can only kill during the night phase!")
        
        if ctx.author.id not in self.alive_players:
            return await ctx.send("Dead players cannot perform actions!")
            
        if self.player_roles[ctx.author.id] != Role.MAFIA:
            return await ctx.send("Only mafia members can use this command!")
            
        if ctx.channel != self.mafia_channel:
            return await ctx.send("This command can only be used in the mafia channel!")

        # Find target player
        target_id = None
        for pid in self.alive_players:
            if self.players[pid].display_name.lower() == target_name.lower():
                target_id = pid
                break
                
        if target_id is None:
            return await ctx.send(f"Could not find player: {target_name}")
            
        # Don't allow mafia to kill themselves
        if target_id == ctx.author.id:
            return await ctx.send("You cannot target yourself!")

        self.night_actions[ctx.author.id] = {
            'action': 'kill',
            'target': target_id
        }
        await ctx.send(f"You have chosen to kill {target_name}")

    async def handle_protect_command(self, ctx, target_name):
        """Handle the protect command from the doctor"""
        if self.state != GameState.NIGHT:
            return await ctx.send("You can only protect during the night phase!")
        
        if ctx.author.id not in self.alive_players:
            return await ctx.send("Dead players cannot perform actions!")
            
        if self.player_roles[ctx.author.id] != Role.DOCTOR:
            return await ctx.send("Only the doctor can use this command!")
            
        if ctx.channel != self.doctor_channel:
            return await ctx.send("This command can only be used in the doctor channel!")

        # Find target player
        target_id = None
        for pid in self.alive_players:
            if self.players[pid].display_name.lower() == target_name.lower():
                target_id = pid
                break
                
        if target_id is None:
            return await ctx.send(f"Could not find player: {target_name}")

        self.night_actions[ctx.author.id] = {
            'action': 'protect',
            'target': target_id
        }
        await ctx.send(f"You have chosen to protect {target_name}")

    async def handle_investigate_command(self, ctx, target_name):
        """Handle the investigate command from the detective"""
        if self.state != GameState.NIGHT:
            return await ctx.send("You can only investigate during the night phase!")
        
        if ctx.author.id not in self.alive_players:
            return await ctx.send("Dead players cannot perform actions!")
            
        if self.player_roles[ctx.author.id] != Role.DETECTIVE:
            return await ctx.send("Only the detective can use this command!")
            
        if ctx.channel != self.detective_channel:
            return await ctx.send("This command can only be used in the detective channel!")

        # Find target player
        target_id = None
        for pid in self.alive_players:
            if self.players[pid].display_name.lower() == target_name.lower():
                target_id = pid
                break
                
        if target_id is None:
            return await ctx.send(f"Could not find player: {target_name}")
            
        # Don't allow detective to investigate themselves
        if target_id == ctx.author.id:
            return await ctx.send("You cannot investigate yourself!")

        self.night_actions[ctx.author.id] = {
            'action': 'investigate',
            'target': target_id
        }
        await ctx.send(f"You have chosen to investigate {target_name}")

    async def check_game_over(self):
        """Check if the game is over and announce winner if so"""
        # Count alive mafia and villagers
        alive_mafia = sum(1 for pid in self.alive_players if self.player_roles[pid] == Role.MAFIA)
        alive_villagers = len(self.alive_players) - alive_mafia  # All non-mafia are counted as villagers

        # Check win conditions
        if alive_mafia >= alive_villagers:
            await self.main_channel.send("🎭 Game Over! The Mafia have won!")
            self.state = GameState.ENDED
            await self.cleanup_game()
            return True
        elif alive_mafia == 0:
            await self.main_channel.send("🎉 Game Over! The Villagers have won!")
            self.state = GameState.ENDED
            await self.cleanup_game()
            return True
        return False

    async def cleanup_game(self):
        """Clean up after game end"""
        # Print out all player roles
        role_reveal = "📜 **Final Role List:**\n"
        for player_id, role in self.player_roles.items():
            player = self.players[player_id]
            status = "☠️ Dead" if player_id in self.dead_players else "😊 Survived"
            role_reveal += f"{player.display_name}: {role.name} ({status})\n"
        
        await self.main_channel.send(role_reveal)
        
        # Delete role channels with error handling
        channels_to_delete = [
            self.mafia_channel,
            self.detective_channel,
            self.doctor_channel
        ]
        
        for channel in channels_to_delete:
            if channel:
                try:
                    await channel.delete()
                except discord.NotFound:
                    print(f"Channel {channel.name} already deleted")
                except Exception as e:
                    print(f"Error deleting channel: {e}")
            
        # Delete the category if it exists
        try:
            category = self.mafia_channel.category if self.mafia_channel else None
            if category:
                await category.delete()
        except discord.NotFound:
            print("Category already deleted")
        except Exception as e:
            print(f"Error deleting category: {e}")
            
        # Reset game state
        self.reset_game_state()
        
        # Send final message with updated command
        await self.main_channel.send(
            "Game has been cleaned up. Start a new game with !startgame"
        )

# Replace the global current_game variable with a dictionary of games
active_games = {}

@bot.command(name='startgame')
async def start_game(ctx):
    """Start a new game of Mafia"""
    guild_id = ctx.guild.id
    
    if guild_id in active_games and active_games[guild_id].state != GameState.WAITING:
        await ctx.send("A game is already in progress in this server!")
        return
        
    game = MafiaGame(ctx.guild, ctx.channel)
    active_games[guild_id] = game
    await ctx.send("Starting a new game of Mafia! Type !join to join the game.\nPlease provide any story context with !context or !contextchat (case sensitive).")

@bot.command(name='context')
async def set_context(ctx, *, context: str):
    """Set the story context for the current game"""
    guild_id = ctx.guild.id
    
    if guild_id not in active_games:
        await ctx.send("No game is currently running! Use !startgame to start a new game.")
        return
        
    if active_games[guild_id].state != GameState.WAITING:
        await ctx.send("Cannot set context after the game has started!")
        return
        
    await active_games[guild_id].set_story_context(context)

@bot.command(name='join')
async def join_game(ctx):
    """Join the current game"""
    guild_id = ctx.guild.id
    
    if guild_id not in active_games:
        await ctx.send("No game is currently running! Use !startgame to start a new game.")
        return
        
    game = active_games[guild_id]
    if game.state != GameState.WAITING:
        await ctx.send("Cannot join a game that has already started!")
        return
        
    if ctx.author.id in game.players:
        await ctx.send("You have already joined the game!")
        return
        
    game.players[ctx.author.id] = ctx.author
    await ctx.send(f"{ctx.author.name} has joined the game!")

@bot.command(name='begin')
async def begin_game(ctx):
    """Begin the game with current players"""
    guild_id = ctx.guild.id
    
    if guild_id not in active_games:
        await ctx.send("No game is currently running! Use !startgame to start a new game.")
        return
        
    await active_games[guild_id].begin_game()

@bot.command(name='endgame')
async def end_game(ctx):
    """End the current game"""
    guild_id = ctx.guild.id
    
    if guild_id not in active_games:
        await ctx.send("No game is currently running!")
        return
        
    # Clean up channels first
    await active_games[guild_id].cleanup_channels()
    
    # Reset game state
    active_games[guild_id].reset_game_state()
    
    # Remove the game from active games
    del active_games[guild_id]
    
    await ctx.send("Game ended. All channels have been cleaned up.")

@bot.command(name='vote')
async def vote(ctx):
    """Legacy vote command - now redirects to the poll system"""
    if ctx.guild is None:
        return
        
    guild_id = ctx.guild.id
    game = active_games.get(guild_id)
    if not game or game.state != GameState.VOTING:
        return
        
    await ctx.send("Please use the poll above to cast your vote! ⬆️")

@bot.command(name='kill')
async def kill(ctx, *, target_name):
    """Command for mafia to kill a player"""
    guild_id = ctx.guild.id
    game = active_games.get(guild_id)
    if game:
        await game.handle_kill_command(ctx, target_name)

@bot.command(name='protect')
async def protect(ctx, *, target_name):
    """Command for doctor to protect a player"""
    guild_id = ctx.guild.id
    game = active_games.get(guild_id)
    if game:
        await game.handle_protect_command(ctx, target_name)

@bot.command(name='investigate')
async def investigate(ctx, *, target_name):
    """Command for detective to investigate a player"""
    guild_id = ctx.guild.id
    game = active_games.get(guild_id)
    if game:
        await game.handle_investigate_command(ctx, target_name)

@bot.command(name='contextchat')
async def set_context_from_chat(ctx, channel_name: str):
    """Set the story context by using messages from a specified channel"""
    guild_id = ctx.guild.id
    
    if guild_id not in active_games:
        await ctx.send("No game is currently running! Use !startgame to start a new game.")
        return
        
    if active_games[guild_id].state != GameState.WAITING:
        await ctx.send("Cannot set context after the game has started!")
        return

    # Find the channel by name
    channel = discord.utils.get(ctx.guild.channels, name=channel_name)
    if not channel:
        await ctx.send(f"Could not find a channel named '{channel_name}'")
        return

    try:
        # Fetch last 100 messages from the channel
        messages = []
        debug_msg = f"\nReading messages from #{channel_name}:\n"
        debug_msg += "-" * 50 + "\n"
        
        async for message in channel.history(limit=100):
            if message.content.strip():  # Only include non-empty messages
                messages.append(message.content)
                debug_msg += f"{message.author.name}: {message.content}\n"
                print(f"Read message: {message.author.name}: {message.content}")  # Debug print
        
        debug_msg += "-" * 50 + "\n"
        debug_msg += f"Total messages read: {len(messages)}\n"
        print(debug_msg)  # Print full debug message
        
        if not messages:
            await ctx.send("No messages found in the channel!")
            return
            
        # Join messages with spaces to create context
        context = " ".join(messages)
        print("\nFinal context being set:")
        print("-" * 50)
        print(context)
        print("-" * 50)
        
        # Set the context in the game
        await active_games[guild_id].set_story_context(context)
        
        await ctx.send(f"Successfully set story context using {len(messages)} messages from #{channel_name}!")
        
    except discord.Forbidden:
        await ctx.send("I don't have permission to read messages in that channel!")
        print(f"Permission error reading from channel {channel_name}")
    except Exception as e:
        print(f"Error setting context from chat: {e}")
        await ctx.send("There was an error fetching messages from that channel.")

@bot.event
async def on_ready():
    print(f'{bot.user} has connected to Discord!')

@bot.command(name='help')
async def help_command(ctx):
    """Display detailed help information about the bot's commands"""
    help_embed = discord.Embed(
        title="🎭 Mafia Game Bot Commands",
        description="A detailed guide to all available commands",
        color=0x2F3136
    )

    # Game Management Commands
    help_embed.add_field(
        name="Game Setup",
        value="""
**!startgame**
Start a new game of Mafia in the current channel.

**!join**
Join the currently waiting game.

**!begin**
Start the game with all current players (minimum 4 required).

**!endgame**
Force end the current game and clean up all channels.
        """,
        inline=False
    )

    # Story Context Commands
    help_embed.add_field(
        name="Story Context",
        value="""
**!context** `<your story>`
Set a custom story context for the game. This will influence how the storyteller generates narratives throughout the game.

**!contextchat** `<channel-name>`
Use the last 100 messages from a specified channel as story context. The channel name must be exact and case-sensitive.
        """,
        inline=False
    )

    # Role-Specific Commands
    help_embed.add_field(
        name="Role Commands (Night Phase Only)",
        value="""
**!kill** `<player-name>` (Mafia Only)
Choose a player to eliminate during the night phase.

**!protect** `<player-name>` (Doctor Only)
Choose a player to protect during the night phase.

**!investigate** `<player-name>` (Detective Only)
Investigate a player to learn their role during the night phase.
        """,
        inline=False
    )

    # Game Rules
    help_embed.add_field(
        name="Game Rules",
        value="""
• Minimum 4 players required to start
• Each night phase lasts 45 seconds (ends early if all actions received)
• Roles are assigned randomly at game start
• The game ends when either all mafia are eliminated (Village wins) or mafia equals/outnumbers villagers (Mafia wins)
        """,
        inline=False
    )

    await ctx.send(embed=help_embed)

# Run the bot
bot.run(os.getenv('DISCORD_TOKEN'))
